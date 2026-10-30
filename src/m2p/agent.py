"""The agent loop: an AI agent (`[ai] engine`) on its own computer works the migration out from upstream mongo2pg.

Three resources: `mongo.computer` holds Mongo and the use case's data (a fresh branch of the seed computer at each
`setup`), `agent.computer` runs the agent, and the agent tries its loads on branches of `postgres.name`, kept until
`clean`. mongo2pg is the release's binary, never built: the agent works with its configuration and the DDL it
generates. A run's end resets the passwords the AI saw; one that passed protects the target and copies it.
"""

import hashlib
import json
import re
import shlex
import signal
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import FrameType
from typing import Annotated, Any, NoReturn

import typer

from core.activity import Activity, events
from core.agent import ai, prompts
from core.agent.build import Builder
from core.agent.frame import Here, remote_app
from core.computer import MONGO_PORT, agent_command, bundled, carried, secret
from core.config import new_run_id
from core.doomed import Doomed, delete, listed_or_deleted
from core.shell import console
from m2p import steps, usecase
from m2p.computer import JOBS_DIR, MONGO_CONTAINER, MONGO_POD, PACKAGE, Computer
from m2p.config import Settings
from m2p.run import AGENT_JOB, Status, status_json

# Copied to the agent computer's LOOP/tools; AGENTS.md goes to LOOP itself.
TOOLS = ("branch.sh",)  # the agent's, in LOOP/tools, with core.agent.ai's; no tool deletes a branch: `clean` does
# What `clean` leaves in LOOP: what `setup` put there.
CLEAN_KEEP = ("AGENTS.md", "tools", "bin", "mongo2pg")
SEED_RECORD = "seed.json"  # on the seed computer, beside seed.py: what its MongoDB was seeded with (`seed_record`)
SNAPSHOT_AGE = 5  # seconds: a branch of a running computer may reuse a disk snapshot that recent
LOADED = "LOADED"  # in LOOP: the id of the run `keep_loaded` copied the target after, once per run


ACTIVITY_LINES = 200  # lines `status` reads, the last of the latest session log
ACTIVITY_CHARS = 300  # of an event, in `status`
SESSIONS = 6  # session lines `status` reads, the latest
BRANCHES_FOR = 30  # seconds `status` keeps its list of the try branches
SESSION_LINE = re.compile(r"^[0-9T:-]+Z session [0-9]+ (started|ended with [0-9]+(, \$[0-9.]+)?)$")  # steps.loop's


@dataclass
class Loop(Here[Settings]):
    poll: float = 20.0

    def computer(self) -> Computer:
        return self._on(self.name)

    def _on(self, name: str) -> Computer:
        return Computer(name, self.nevia, self.poll, settings=self.settings)

    # ------------------------------------------------------------------ setup (Nevia)

    def setup(self) -> None:
        """On the agent computer: everything the agent needs, Mongo, Postgres, its tools and its link to Mongo.

        mongo2pg is the release's binary, downloaded there. Each step checks what is already there, so a setup
        that stopped halfway can be repeated; but the source is replaced each time by a fresh branch of the seed
        computer, seeded with [m2p.seed] as it is now (`source`), so not while the agent runs on it.
        """
        s = self.settings
        if self.computer().job_running(AGENT_JOB):
            raise RuntimeError(f"the agent is running on {self.name}; wait for it to stop, or stop it, first")
        Builder(s.mongo2pg).install(steps.loop_dir(s) / "bin/mongo2pg")  # a missing release: first
        self.source()
        self.postgres()
        self.nevia.computers.update(self.name, env={"DATABASE_URL": secret(s.secret(s.postgres.secret))})
        pc = self.computer()
        self._upload_tools(pc)
        steps.setup(s)
        exists = f"SELECT 1 FROM pg_database WHERE datname = '{s.mongo.database}'"  # noqa: S608 - validated identifier
        if not pc.psql(exists).strip():
            pc.psql(f"CREATE DATABASE {s.mongo.database}")
        self.link()

    def _env_sh(self) -> bytes:
        """The tools' settings: the target database, and the prefix of its branches."""
        pg, tries = shlex.quote(self.settings.postgres.name), shlex.quote(self.settings.tries)
        return f'export PATH="$HOME/.local/bin:$PATH"\nexport M2P_PG={pg}\nexport M2P_TRIES={tries}\n'.encode()

    def _upload_tools(self, pc: Computer) -> None:
        """The tools read-only, so the agent can't quietly change how its work is checked."""
        s = self.settings
        mine = {name: (bundled(PACKAGE, f"scripts/{name}"), "555" if name.endswith(".sh") else "444") for name in TOOLS}
        pc.put_tools(f"{self.dir}/tools", {**mine, "env.sh": (self._env_sh(), "444"), **ai.tools()})
        guide = prompts.prompt("m2p", "AGENTS.md").format(
            pg=s.postgres.name,
            mongo=s.mongo.computer,
            database=s.mongo.database,
            namespace=s.mongo.database,
            sample=f"{s.mongo2pg.sample_percent:g}",
            dir=self.dir,
            mongo2pg=s.mongo2pg.release,
            data=usecase.DESCRIPTION,
        )
        pc.upload(guide.encode(), f"{self.dir}/AGENTS.md", "444")

    # ------------------------------------------------------------------ the source and the target

    def source(self) -> None:
        """`mongo.computer` replaced by a fresh branch of the seed computer (`seeded`), so that every setup gives
        the agent the same data; MongoDB started there (a branch has the data, not the processes). The old source
        goes first, with the agent's links to it; the seed computer then sleeps."""
        s, nv = self.settings, self.nevia
        name, seed = s.mongo.computer, s.seed_computer
        self.seeded()
        if nv.computers.exists(name):
            self._on(name).awake()
            for made in self.tunnels.links(name):  # their other end, on the agent computer, holds MONGO_PORT
                self.tunnels.unlink(name, made.id)
            nv.computers.delete(name)
            console.log(f"deleted computer {name}, the last setup's source")
        console.log(f"branching {seed} into {name}")
        nv.computers.branch(seed, name)
        self._on(seed).sleep()
        self.mongo_up(name)
        console.log(f"{name}: MongoDB up, database {s.mongo.database}, a fresh copy of {seed}")

    def seeded(self) -> None:
        """The seed computer, its MongoDB seeded as the settings are now (`seed_record`, its SEED_RECORD), stopped
        and its disk settled for a branch. One seeded otherwise, or not to the end (no record), is made again:
        deleted, then made from scratch."""
        seed, want = self.settings.seed_computer, self.seed_record()
        pc = self._on(seed)
        if self.nevia.computers.exists(seed):
            pc.awake()
            if pc.sh(f"cat ~/{SEED_RECORD} 2>/dev/null || true").strip() == want.decode().strip():
                console.log(f"{seed}: seeded with [m2p.seed] as it is, kept")
            else:
                self.nevia.computers.delete(seed)
                console.log(f"deleted computer {seed}: seeded otherwise, or not to the end")
        if not self.nevia.computers.exists(seed):
            self.provision(seed)
            pc.upload(want, SEED_RECORD, "444")  # last: a seed that stopped halfway has none
        # mongod stopped, its files written out; then no write for SNAPSHOT_AGE, so that the branch has them all.
        pc.sh(f"podman stop -i {MONGO_CONTAINER} > /dev/null; sync")
        time.sleep(SNAPSHOT_AGE)

    def seed_record(self) -> bytes:
        """What the seed computer's data comes from: [m2p.seed], the database, MongoDB's image (which wrote its
        files) and seed.py's sha256 (the same settings give the same documents with the same seed.py), as JSON.
        A file on its disk, not a computer env var: the data and what it was made with go together into a branch,
        and Nevia shows no env value, so reading either takes an exec anyway."""
        s = self.settings
        record = {"seed": s.seed.model_dump(), "database": s.mongo.database, "image": s.mongodb.image,
                  "seed.py": hashlib.sha256(usecase.file("seed.py")).hexdigest()}  # fmt: skip
        return (json.dumps(record, sort_keys=True) + "\n").encode()

    def provision(self, name: str) -> None:
        """Create a computer running MongoDB with the use case's data, generated there by its seed.py.

        Every step checks what is already there, so a provision that stopped halfway can be repeated; the data is
        seeded from scratch (seed.py drops the database first).
        """
        if not self.nevia.computers.exists(name):
            self.nevia.computers.create(name)
        self.mongo_up(name)
        self.seed(name)
        console.log(f"{name}: MongoDB up, database {self.settings.mongo.database}")

    def mongo_up(self, name: str) -> None:
        """MongoDB started on the computer (`core.computer.Computer.mongo_up`), its scripts and settings sent."""
        pc = self._on(name)
        pc.put_mongo(self.settings.mongodb, pod=MONGO_POD, container=MONGO_CONTAINER)
        pc.mongo_up(self.settings.mongodb.image)

    def seed(self, name: str) -> None:
        """Replace the data in the computer's MongoDB with the use case's seed.py's, from [m2p.seed].

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
        self.nevia.databases.store_uri(pg.name, name)
        console.log(f"secret {name} holds the {pg.name} URI")

    # ------------------------------------------------------------------ the agent computer

    def link(self) -> None:
        """Mongo on the source computer at 127.0.0.1:MONGO_PORT on the agent computer, unless a live link is there."""
        source = self.settings.mongo.computer
        for made in self.tunnels.links(source):
            if made.source == self.name and made.local_port == MONGO_PORT:
                if made.alive:
                    console.log(f"{self.name} reaches Mongo on {source} through link {made.id}")
                    return
                self.tunnels.unlink(source, made.id)
        made = self.tunnels.link(self.name, source, MONGO_PORT, container=MONGO_CONTAINER)
        console.log(f"{self.name} reaches Mongo on {source} through link {made.id} for {self.settings.agent.hours} h")

    # ------------------------------------------------------------------ the run

    def run(self) -> None:
        """Start the agent and return; `status` follows it."""
        pc = self.computer()
        if pc.job_running(AGENT_JOB):
            raise RuntimeError(f"the agent is already running on {self.name}; it stops after agent.hours")
        # This repository's guide and tools, whatever an earlier setup sent.
        self._upload_tools(pc)
        self.link()
        s, lp = self.settings, self.settings.agent
        pc.upload(f"{new_run_id()}\n".encode(), f"{self.dir}/RUN", "644")  # the run's id, for its results
        env = carried(ai.env(s.ai, s.secret(s.ai.gateway_secret)))  # the sessions' logs in your time zone
        loop = agent_command("m2p", "step", "loop", str(lp.sessions))  # the sessions, with the gateway key
        pc.start_job(AGENT_JOB, f"timeout {lp.hours}h {loop}", env=env)

    def stop(self) -> None:
        """Stop the agent and anything it left running; its branches and files stay. The run's end (`ended`) again,
        should the job have been killed before its own was over."""
        pc = self.computer()
        running = pc.job_running(AGENT_JOB)
        pc.stop_job(AGENT_JOB)  # the loop, its AI sessions and what they started: the job's session
        # What would have left that session on its own: the engine's binary, a load.
        pc.sh(f"pkill -x {ai.binary(self.settings.ai)} || true; pkill -x mongo2pg || true")
        console.log(f"{self.name}: agent stopped")
        if running:
            self.ended()

    # ------------------------------------------------------------------ a run's end

    def ended(self) -> None:
        """A run's end, however it ended (the loop job's own, or `stop`'s): `lock`, then `keep_loaded` if it passed
        (DONE.md). Safe to redo."""
        self.lock()
        if (steps.loop_dir(self.settings) / "DONE.md").is_file():
            self.keep_loaded()

    def lock(self) -> None:
        """New passwords for every try branch still there and the target, so that each copy of their URIs the AI
        saw (tries/*.uri, its projects' configs, its logs) stops working; the target's new URI into its secret (the
        agent computer's DATABASE_URL, `clean`'s psql and the next run read it), and the tries' URI files deleted.
        No password is printed."""
        s, nv = self.settings, self.nevia
        reset = self._branches()
        for name in reset:
            nv.databases.reset_password(name)
        if nv.databases.exists(s.postgres.name):
            nv.databases.reset_password(s.postgres.name)
            nv.databases.store_uri(s.postgres.name, s.secret(s.postgres.secret))
            reset.append(s.postgres.name)
        for f in (steps.loop_dir(s) / "tries").glob("*.uri"):
            if s.is_try(f.stem):
                f.unlink()
        if reset:
            console.log(f"new passwords for {', '.join(reset)}: the URIs the AI saw no longer work")

    def keep_loaded(self) -> None:
        """After a run that passed: the target protected, and copied into `loaded` (protected too), a restore point
        to keep until the cut-over; an older copy goes first. The copy once per run (LOOP/LOADED has its id)."""
        s, nv, d = self.settings, self.nevia, steps.loop_dir(self.settings)
        nv.databases.protect(s.postgres.name)
        run = _text(d / "RUN").strip() if (d / "RUN").is_file() else ""
        if run and (d / LOADED).is_file() and _text(d / LOADED).strip() == run:
            return
        if nv.databases.exists(s.loaded):
            nv.databases.protect(s.loaded, False)
            nv.databases.delete(s.loaded)
        nv.databases.branch(s.postgres.name, s.loaded, protected=True)
        (d / LOADED).write_text(f"{run}\n")
        console.log(f"{s.postgres.name} protected, and copied into {s.loaded} (protected): the passing load")

    def status(self) -> Status:
        """The loop as LOOP's files have it (this computer's): DONE.md, the sessions' start and end lines, what the
        agent did in the latest session (`core.activity.events` of its log's end), and ATTEMPTS.md."""
        d, pc = steps.loop_dir(self.settings), self.computer()
        logs = sorted((d / "logs").glob("ai-*.log"), key=lambda p: (p.stat().st_mtime, p.name))  # oldest first
        sessions = [line for log in logs for line in _lines(log) if SESSION_LINE.match(line)]
        acts: list[Activity] = []
        if logs:
            lines = _lines(logs[-1])
            start = max(len(lines) - ACTIVITY_LINES, 0)
            acts = [replace(a, text=a.text[:ACTIVITY_CHARS]) for a in events(lines[start:], logs[-1].name, start)]
        attempts = d / "ATTEMPTS.md"
        return Status(
            running=pc.job_running(AGENT_JOB),
            done=(d / "DONE.md").is_file(),
            sessions="\n".join(sessions[-SESSIONS:]),
            attempts=_text(attempts).strip() if attempts.is_file() else "",
            branches=self._recent_branches(),
            memory=pc.memory(),
            activity=acts,
        )

    def _branches(self, dbs: list[dict[str, Any]] | None = None) -> list[str]:
        """The agent's branches of the target, one per try (`Settings.is_try`), of `dbs` or of the workspace."""
        dbs = self.nevia.databases.list() if dbs is None else dbs
        return sorted(db["name"] for db in dbs if self.settings.is_try(db["name"]))

    def _recent_branches(self) -> list[str]:
        """The same for `status`, which your machine asks for every few seconds while it follows a run: kept
        BRANCHES_FOR seconds in the jobs directory, as each listing costs a sign-in (`core.nevia.THROTTLED`)."""
        cache = steps.HOME / JOBS_DIR.removeprefix("$HOME/") / "branches.json"
        try:
            if time.time() - cache.stat().st_mtime < BRANCHES_FOR:
                return json.loads(cache.read_text())
        except OSError, ValueError:
            pass
        names = self._branches()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(names))
        return names

    def doomed_clean(self) -> Doomed:
        """What `clean` deletes: the try branches, and LOOP's files (all but what `setup` put there)."""
        if self.computer().job_running(AGENT_JOB):
            raise RuntimeError(f"the agent is running on {self.name}; wait for it to stop first")
        return Doomed(databases=self._branches(), dirs=[self.dir])

    def clean(self, d: Doomed) -> None:
        """Remove the last run's work, for a fresh run on the same setup: what `doomed_clean` lists, and the
        target's schema.

        On the agent computer, LOOP keeps only what `setup` put there (CLEAN_KEEP), with the checkout
        back at the release and ATTEMPTS.md empty. The computers, secrets and Mongo's data stay.
        """
        delete(self.nevia, d, files=self._reset)
        s = self.settings
        # Identifiers validated in agent.toml.
        self.computer().psql(f"DROP SCHEMA IF EXISTS {s.mongo.database} CASCADE", database=s.mongo.database)
        console.log(f"{self.name}: schema {s.mongo.database} dropped from {s.postgres.name}")

    def _reset(self, _d: Doomed) -> None:
        """LOOP back to its setup (`steps.reset`), and the agent's job forgotten."""
        steps.reset(self.settings, CLEAN_KEEP)
        for f in (steps.HOME / JOBS_DIR.removeprefix("$HOME/")).glob(f"{AGENT_JOB}.*"):
            f.unlink()
        console.log(f"{self.name}: ~/{self.dir} back to its setup")

    # ------------------------------------------------------------------ destroy

    def doomed(self) -> Doomed:
        """What the agent deletes in `destroy`, of what exists: only the names in the settings and those made from
        them (the seed computer, the try branches, the target's copy). The agent computer, its principal and the
        secrets of `setup` are your machine's to delete (`Bootstrap`)."""
        s, nv = self.settings, self.nevia
        dbs = nv.databases.list()
        have = {db["name"]: db for db in dbs}
        databases = [*self._branches(dbs), *(n for n in (s.loaded, s.postgres.name) if n in have)]
        pg_secret = s.secret(s.postgres.secret)
        return Doomed(
            computers=[c for c in (s.mongo.computer, s.seed_computer) if nv.computers.exists(c)],
            databases=databases,
            secrets=[pg_secret] if nv.secrets.exists(pg_secret) else [],
            unprotected=[n for n in databases if have[n].get("protected")],
        )

    def destroy(self, doomed: Doomed) -> None:
        """Delete everything in `doomed` (`core.doomed.delete`): the branches before the database they come from,
        a protected one once its protection is cleared, and the agent's link to Mongo with Mongo's computer."""
        delete(self.nevia, doomed)


def _text(path: Path) -> str:
    """A file the agent may be writing: a character cut at its end is replaced, not an error."""
    return path.read_bytes().decode(errors="replace")


def _lines(path: Path) -> list[str]:
    return _text(path).splitlines()


# ---------------------------------------------------------------------- the agent computer's commands
# Your machine's commands (m2p.local) run these there, through `agent m2p remote`.

remote = remote_app()


def _here() -> Loop:
    return Loop.of(Settings.load())


@remote.command("setup")
def remote_setup() -> None:
    """Mongo, Postgres, the agent's tools and its link to Mongo."""
    _here().setup()


@remote.command("run")
def remote_run() -> None:
    """Start the agent loop."""
    _here().run()


@remote.command("status")
def remote_status() -> None:
    print(status_json(_here().status()))  # noqa: T201 - read by your machine


@remote.command("stop")
def remote_stop() -> None:
    _here().stop()


@remote.command("clean")
def remote_clean(yes: Annotated[bool, typer.Option("--yes", "-y")] = False) -> None:
    """Without --yes, what it would delete, as JSON."""
    lp = _here()
    listed_or_deleted(lp.doomed_clean(), yes=yes, delete=lp.clean)


@remote.command("destroy")
def remote_destroy(yes: Annotated[bool, typer.Option("--yes", "-y")] = False) -> None:
    """Without --yes, what it would delete, as JSON."""
    lp = _here()
    listed_or_deleted(lp.doomed(), yes=yes, delete=lp.destroy)


@remote.command("step")
def remote_step(name: str, arg: str) -> None:
    """A step that needs a secret, which this job's env holds: loop (`arg`: the sessions), then the run's end
    (`Loop.ended`) however the sessions ended, a TERM included (`stop`'s, or `timeout`'s at agent.hours)."""
    if name != "loop":
        raise typer.BadParameter(f"no step {name!r}")
    signal.signal(signal.SIGTERM, _terminated)
    try:
        outcome = steps.loop(Settings.load(), int(arg))
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # the end goes to its end
        _here().ended()
    print(outcome)  # noqa: T201 - the job's last line


def _terminated(signum: int, _frame: FrameType | None) -> NoReturn:
    raise SystemExit(128 + signum)  # through `remote_step`'s finally
