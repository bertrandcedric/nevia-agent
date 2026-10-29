"""The agent loop: an AI agent (`[ai] engine`) on its own computer works the migration out from upstream mongo2pg.

Three resources: `mongo.computer` holds Mongo and the use case's data, `agent.computer` runs the agent, and
the agent tries its loads on branches of `postgres.name`, kept until `clean`. mongo2pg is the release's binary,
never built: the agent works with its configuration and the DDL it generates.
"""

import json
import re
import shlex
from dataclasses import asdict, dataclass, field
from typing import Annotated

import typer
from nevia_kit import Tunnels

from core.activity import Activity, event
from core.agent import ai, prompts
from core.agent.build import Builder
from core.computer import agent_command
from core.doomed import Doomed
from core.nevia import Nevia
from core.shell import console
from m2p import config, steps, usecase
from m2p.computer import JOBS_DIR, MONGO_CONTAINER, Computer
from m2p.config import Settings

AGENT_JOB = "agent"
# Copied to the agent computer's LOOP/tools; AGENTS.md goes to LOOP itself.
TOOLS = ("branch.sh",)  # the agent's, in LOOP/tools, with core.agent.ai's; no tool deletes a branch: `clean` does
# What `clean` leaves in LOOP: what `setup` put there.
CLEAN_KEEP = ("AGENTS.md", "tools", "bin", "mongo2pg")


ACTIVITY_LINES = 200  # lines `status` reads, the last of the latest session log
SEP = "@@m2p@@"  # between the parts of `status`'s output; ATTEMPTS.md may hold `---` lines
SESSION_LINE = shlex.quote(r"^[0-9T:-]+Z session [0-9]+ (started|ended with [0-9]+)$")  # from steps.loop


def activity(lines: str) -> list[Activity]:
    """What the agent did, from the `NAME:N:HH:MM:SS text` lines `status` takes from a session log that
    tools/render-<engine>.jq wrote: its intents, commands, edits, messages, failures and errors.
    """
    out = []
    for line in lines.splitlines():
        if m := re.match(r"^([^:]+):(\d+):(\d\d:\d\d:\d\d) (.*)$", line):
            name, n, time_, text = m.groups()
            out.append(event(f"{name}:{n}", time_, text[:300]))  # the times are the agent computer's, in UTC
    return out


@dataclass(frozen=True)
class Status:
    running: bool
    done: bool
    sessions: str
    attempts: str
    branches: list[str]
    memory: dict[str, str]
    activity: list[Activity] = field(default_factory=list)


def status_json(st: Status) -> str:
    """A status as the agent sends it to your machine."""
    return json.dumps(asdict(st))


def status_of(text: str) -> Status:
    d = json.loads(text)
    acts = [Activity(**a) for a in d.get("activity", [])]
    return Status(d["running"], d["done"], d["sessions"], d["attempts"], d["branches"], d["memory"], acts)


@dataclass
class Loop:
    settings: Settings
    nevia: Nevia
    poll: float = 20.0
    tunnels: Tunnels = field(init=False)

    def __post_init__(self) -> None:
        self.tunnels = Tunnels(self.nevia.kit, lifetime=int(self.settings.agent.hours * 3600))

    @property
    def name(self) -> str:
        return self.settings.agent.computer

    @property
    def dir(self) -> str:
        return self.settings.agent.dir

    def computer(self) -> Computer:
        return self._on(self.name)

    def _on(self, name: str) -> Computer:
        return Computer(name, self.nevia, self.poll, settings=self.settings)

    # ------------------------------------------------------------------ setup (Nevia)

    def setup(self) -> None:
        """On the agent computer: everything the agent needs, Mongo, Postgres, its tools and its link to Mongo.

        mongo2pg is the release's binary, downloaded there. Each step checks what is already there, so a setup
        that stopped halfway can be repeated.
        """
        s = self.settings
        Builder(s.mongo2pg).install(steps.loop_dir(s) / "bin/mongo2pg")  # a missing release: first
        self.provision(s.mongo.computer)  # it checks each step, so a source computer left halfway gets finished
        self.postgres()
        self.nevia.computers.update(self.name, env={"DATABASE_URL": f"{{{{secrets.{s.secret(s.postgres.secret)}}}}}"})
        pc = self.computer()
        self._upload_tools(pc)
        steps.setup(s)
        exists = f"SELECT 1 FROM pg_database WHERE datname = '{s.mongo.database}'"  # noqa: S608 - validated identifier
        if not pc.psql(exists).strip():
            pc.psql(f"CREATE DATABASE {s.mongo.database}")
        self.link()

    def _env_sh(self) -> bytes:
        pg = shlex.quote(self.settings.postgres.name)
        return f'export PATH="$HOME/.local/bin:$PATH"\nexport M2P_PG={pg}\n'.encode()

    def _upload_tools(self, pc: Computer) -> None:
        """The tools read-only, so the agent can't quietly change how its work is checked."""
        s = self.settings
        # Replaced as a whole, so that tools an earlier version had go too.
        pc.put_lib()
        t = f"~/{self.dir}/tools"
        pc.sh(f"chmod -R u+w {t} 2>/dev/null; rm -rf {t} && mkdir -p {t}")
        for name in TOOLS:
            pc.put(name, f"{self.dir}/tools/{name}", "555" if name.endswith(".sh") else "444")
        pc.upload(self._env_sh(), f"{self.dir}/tools/env.sh", "444")
        for name, data in ai.tools().items():
            pc.upload(data, f"{self.dir}/tools/{name}", "444")
        guide = prompts.prompt("m2p", "AGENTS.md").format(
            pg=s.postgres.name,
            mongo=s.mongo.computer,
            database=s.mongo.database,
            namespace=s.mongo.database,
            dir=self.dir,
            mongo2pg=s.mongo2pg.release,
            data=usecase.DESCRIPTION,
        )
        pc.upload(guide.encode(), f"{self.dir}/AGENTS.md", "444")

    def _uri(self, database: str) -> str:
        """The database's connection URI, password included: it goes to a secret or a file, never a log."""
        uri = self.nevia.databases.connection(database)["uri"]
        if not uri:
            raise RuntimeError(f"{database} has no connection URI yet")
        return uri

    # ------------------------------------------------------------------ the source and the target

    def provision(self, name: str) -> None:
        """Create a computer running MongoDB with the use case's data, generated there by its seed.py.

        Every step checks what is already there, so a run that stopped halfway can be repeated, and
        data already there stays.
        """
        if not self.nevia.computers.exists(name):
            self.nevia.computers.create(name)
        pc = self._on(name)
        image = self.settings.mongodb.image
        pc.put_mongo({"MONGO_IMAGE": image, "MONGO_CACHE_GB": str(self.settings.mongodb.cache_gb), "MONGO_POD": "m2p",
                      "MONGO_CONTAINER": MONGO_CONTAINER})  # fmt: skip
        pc.mongo_up(image)
        db = self.settings.mongo.database
        if int(pc.mongo(f"print(db.{usecase.COLLECTION}.countDocuments())").strip().splitlines()[-1]) == 0:
            self.seed(name)
        console.log(f"{name}: MongoDB up, database {db}")

    def seed(self, name: str) -> None:
        """Replace the data in the computer's MongoDB with the use case's seed.py's, from [seed].

        seed.py runs on the computer, in the pod's network namespace: Mongo listens only there.
        """
        self._on(name).upload(usecase.file("seed.py"), "seed.py")
        args = usecase.seed_args(self.settings.seed, self.settings.mongo.database)
        pid = f"$(podman inspect -f '{{{{.State.Pid}}}}' {MONGO_CONTAINER})"
        log = self._on(name).run_job("seed", f'podman unshare nsenter -t "{pid}" -n python3 ~/seed.py {args}')
        for line in log.strip().splitlines():
            if line.startswith("[seed]"):
                console.log(f"{name}: {line}")

    def postgres(self) -> None:
        """Create the Postgres database and store its URI as a workspace secret."""
        pg = self.settings.postgres
        if not self.nevia.databases.exists(pg.name):
            self.nevia.databases.create(pg.name)
        name = self.settings.secret(pg.secret)
        self.nevia.secrets.set(name, self._uri(pg.name))
        console.log(f"secret {name} holds the {pg.name} URI")

    # ------------------------------------------------------------------ the agent computer

    def link(self) -> None:
        """Mongo on the source computer at 127.0.0.1:27017 on the agent computer, unless a live link is there."""
        source = self.settings.mongo.computer
        for made in self.tunnels.links(source):
            if made.source == self.name and made.local_port == 27017:
                if made.alive:
                    console.log(f"{self.name} reaches Mongo on {source} through link {made.id}")
                    return
                self.tunnels.unlink(source, made.id)
        made = self.tunnels.link(self.name, source, 27017, container=MONGO_CONTAINER)
        console.log(f"{self.name} reaches Mongo on {source} through link {made.id} for {self.settings.agent.hours} h")

    # ------------------------------------------------------------------ the run

    def running(self) -> bool:
        return self.computer().job_running(AGENT_JOB)

    def run(self) -> None:
        """Start the agent and return; `status` follows it."""
        pc = self.computer()
        if pc.job_running(AGENT_JOB):
            raise RuntimeError(f"the agent is already running on {self.name}; it stops after agent.hours")
        # This repository's guide and tools, whatever an earlier setup sent.
        self._upload_tools(pc)
        self.link()
        s, lp = self.settings, self.settings.agent
        pc.sh(f"date -u +%y%m%d%H%M%S > ~/{self.dir}/RUN")  # the run's id, for its results
        env = ai.env(s.ai, s.secret(s.ai.gateway_secret))
        loop = agent_command("m2p", "step", "loop", str(lp.sessions))  # the sessions, with the gateway key
        pc.start_job(AGENT_JOB, f"timeout {lp.hours}h {loop}", env=env)

    def stop(self) -> None:
        """Stop the agent and anything it left running; its branches and files stay."""
        # [r]: the patterns must not match the shell running them, or pkill kills it too (exit 143). The
        # job's wrapper is left alone, so it still writes the exit code `run` waits for.
        self.computer().sh(
            "pkill -f '^timeout [^ ]+ .*m2p remote step [l]oop' || true; "
            # The step itself, as bin/agent execs it: `.../python -m core m2p remote step loop N`. The wrapper's
            # `bash -lc timeout ...` must not match.
            "pkill -f '^[^ ]*python[^ ]* -m core m2p remote step [l]oop' || true; "
            "pkill -x claude || true; pkill -x copilot || true; pkill -x mongo2pg || true"
        )
        console.log(f"{self.name}: agent stopped")

    def status(self) -> Status:
        d, pc = self.dir, self.computer()
        out = pc.sh(
            f"cd ~/{d}; test -f DONE.md && echo done=1 || echo done=0; "
            # The start and end lines steps.loop writes in each session's log, oldest session first.
            f"echo {SEP}; ls -tr logs/session-*.log 2>/dev/null | xargs -r grep -hE {SESSION_LINE} | tail -n 6; "
            f"echo {SEP}; latest=$(ls -t logs/session-*.log 2>/dev/null | head -1); "
            # An event's first line starts with its time; the lines under it are its output.
            f'[ -z "$latest" ] || grep -nE "^[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}} " "$latest" | tail -n {ACTIVITY_LINES} '
            f'| sed "s|^|$(basename "$latest"):|"; '
            f"echo {SEP}; cat ATTEMPTS.md 2>/dev/null"
        )
        head, sessions, events, attempts = [*out.split(f"{SEP}\n", 3), "", "", ""][:4]
        facts = dict(line.split("=", 1) for line in head.split() if "=" in line)
        branches = sorted(db["name"] for db in self.nevia.databases.list() if db["name"].startswith("m2p-try"))
        return Status(
            running=pc.job_running(AGENT_JOB),
            done=facts.get("done") == "1",
            sessions=sessions.strip(),
            attempts=attempts.strip(),
            branches=branches,
            memory=pc.memory(),
            activity=activity(events),
        )

    def clean(self) -> list[str]:
        """Remove the last run's work, for a fresh run on the same setup; returns the branches deleted.

        On the agent computer, LOOP keeps only what `setup` put there (CLEAN_KEEP), with the checkout
        back at the release and ATTEMPTS.md empty; the `m2p-try*` branches go, and the target's schema.
        The computers, secrets and Mongo's data stay.
        """
        pc = self.computer()
        if pc.job_running(AGENT_JOB):
            raise RuntimeError(f"the agent is running on {self.name}; wait for it to stop first")
        keep = " ".join(f"! -name {shlex.quote(k)}" for k in CLEAN_KEEP)
        release = shlex.quote(self.settings.mongo2pg.release)
        pc.sh(
            f"set -euo pipefail; cd ~/{self.dir}; find . -mindepth 1 -maxdepth 1 {keep} -exec rm -rf {{}} +; "
            "mkdir -p tries logs; printf '# Attempts\\n' > ATTEMPTS.md; "
            f"git -C mongo2pg switch -q -f -C agent {release}; git -C mongo2pg clean -q -fdx; "
            f"rm -rf {JOBS_DIR}/{AGENT_JOB}.*"
        )
        branches = sorted(db["name"] for db in self.nevia.databases.list() if db["name"].startswith("m2p-try"))
        for branch in branches:
            self.nevia.databases.delete(branch)
        s = self.settings
        # Identifiers validated in agent.toml.
        pc.psql(f"DROP SCHEMA IF EXISTS {s.mongo.database} CASCADE", database=s.mongo.database)
        console.log(
            f"{self.name}: ~/{self.dir} back to its setup; branches deleted: {', '.join(branches) or 'none'}; "
            f"schema {s.mongo.database} dropped from {s.postgres.name}"
        )
        return branches

    # ------------------------------------------------------------------ destroy

    def doomed(self) -> Doomed:
        """What the agent deletes in `destroy`, of what exists: only the names in the settings. The agent computer,
        its principal and the secrets of `setup` are your machine's to delete (`Bootstrap`)."""
        s, nv = self.settings, self.nevia
        branches = sorted(db["name"] for db in nv.databases.list() if db["name"].startswith("m2p-try"))
        pg_secret = s.secret(s.postgres.secret)
        return Doomed(
            computers=[s.mongo.computer] if nv.computers.exists(s.mongo.computer) else [],
            databases=[*branches, *([s.postgres.name] if nv.databases.exists(s.postgres.name) else [])],
            secrets=[pg_secret] if any(sec["name"] == pg_secret for sec in nv.secrets.list()) else [],
        )

    def destroy(self, doomed: Doomed) -> None:
        """Delete everything in `doomed`: the branches before the database they come from."""
        nv = self.nevia
        for name in doomed.computers:
            nv.computers.delete(name)  # the agent's link to Mongo goes with it
            console.log(f"deleted computer {name}")
        for name in doomed.databases:
            nv.databases.delete(name)
            console.log(f"deleted database {name}")
        for name in doomed.secrets:
            nv.secrets.delete(name)
            console.log(f"deleted secret {name}")


# ---------------------------------------------------------------------- the agent computer's commands
# Your machine's commands (m2p.local) run these there, through `agent m2p remote`.

remote = typer.Typer(no_args_is_help=True, hidden=True, help="On the agent computer: what the commands do there.")


def _here() -> Loop:
    s = config.load()
    return Loop(s, Nevia(s.nevia.workspace))


@remote.command("setup")
def remote_setup() -> None:
    """Mongo, Postgres, the agent's tools and its link to Mongo."""
    _here().setup()


@remote.command("run")
def remote_run() -> None:
    """Start the agent loop."""
    _here().run()


@remote.command("running")
def remote_running() -> None:
    print("yes" if _here().running() else "no")  # noqa: T201 - read by your machine


@remote.command("status")
def remote_status() -> None:
    print(status_json(_here().status()))  # noqa: T201 - read by your machine


@remote.command("stop")
def remote_stop() -> None:
    _here().stop()


@remote.command("clean")
def remote_clean() -> None:
    _here().clean()


@remote.command("destroy")
def remote_destroy(yes: Annotated[bool, typer.Option("--yes", "-y")] = False) -> None:
    """Without --yes, what it would delete, as JSON."""
    lp = _here()
    if yes:
        lp.destroy(lp.doomed())
    else:
        print(lp.doomed().to_json())  # noqa: T201 - read by your machine


@remote.command("step")
def remote_step(name: str, arg: str) -> None:
    """A step that needs a secret, which this job's env holds: loop (`arg`: the sessions)."""
    if name != "loop":
        raise typer.BadParameter(f"no step {name!r}")
    print(steps.loop(config.load(), int(arg)))  # noqa: T201 - the job's last line
