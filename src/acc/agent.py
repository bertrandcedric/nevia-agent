"""The agent: Claude Code on its own Nevia computer changes the app, and the change goes to a pull request once a
sandbox has passed.

This runs on the agent computer: it makes the databases, the checkpoint and the sandbox with nevia-kit, and does
the work itself (`acc.steps`); a step that needs a secret runs as a job (`agent acc remote step ...`), with the
secret in its env. Each step is recorded once done, in build/acc-runs/<run>/state.json, so `agent acc run resume`
takes a stopped or failed run up from where it stopped.
"""

import io
import json
import os
import re
import shlex
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import rich.console
import typer
from nevia_kit import Nevia, NeviaError, Tunnels
from pydantic import BaseModel

from acc import config, pr, steps
from acc.computer import JOBS_DIR, Computer, redact, secret
from acc.config import Settings
from acc.stack import CA_FILE, Kind, Stack, sandbox_name
from acc.steps import CHECK
from core.activity import events
from core.agent import prompts
from core.agent.build import Builder
from core.computer import agent_command, bundled
from core.config import BUILD_DIR
from core.doomed import Doomed
from core.shell import console

RunKind = Literal["migrate", "task"]
RUNS_DIR = BUILD_DIR / "acc-runs"
TOOLS = ("render.jq",)  # in DIR/tools, read-only, with core's common.jq: how Claude's events are rendered
STEPS: dict[RunKind, tuple[str, ...]] = {
    "migrate": ("clone", "stack", "analysis", "claude", "build", "sandbox", "pr"),
    "task": ("clone", "stack", "claude", "build", "sandbox", "feature-check", "pr"),
}
ANALYSIS_LINK_SECONDS = 3600  # the agent's link to MongoDB, for mongo2pg infer only
RUN_ID = re.compile(r"^\d{12}$")
RUN_JOB = "acc-run"  # a run's orchestrator, on the agent computer
SUFFIXED = re.compile(r"-(\d{12})$")
CHECKPOINT = re.compile(r"acc-(\d{12})")  # of the app, for a run's sandbox


def prompt(kind: RunKind, run_dir: str, task: str = "", *, repo: str = "", branch: str = "") -> str:
    """Claude's task: the migration's, or the feature asked for wrapped in the task's. `{{...}}` are replaced as
    text, the request last, so that its own braces stay as they are."""
    text = prompts.prompt("acc", "MIGRATE.md" if kind == "migrate" else "FEATURE.md")
    for key, value in {"REPO": repo, "BRANCH": branch, "RUN_DIR": run_dir, "CHECK": f"{run_dir}/{CHECK}"}.items():
        text = text.replace("{{" + key + "}}", value)
    return text.replace("{{TASK}}", task.strip())


def claude_env(s: Settings) -> dict[str, str]:
    """What Claude's step gets in its env: the gateway and its key as a secret template, the gateway's model
    names; its other settings come from agent.toml."""
    c = s.claude
    env: dict[str, str] = {}
    if c.gateway_url:
        env["ANTHROPIC_BASE_URL"] = c.gateway_url
        env["ANTHROPIC_AUTH_TOKEN"] = secret(s.secret(c.gateway_secret))
    for alias, model in c.gateway_models.model_dump().items():
        if model:
            env[f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL"] = model
    if tz := os.environ.get("TZ"):
        env["TZ"] = tz  # the orchestrator's, your machine's: the log's times as your clock reads them
    return env


class RunState(BaseModel):
    """A run as `run` left it; its steps are done in order, each once."""

    id: str
    kind: RunKind
    task: str = ""
    started: str
    done: list[str] = []
    durations: dict[str, float] = {}
    outcome: str = ""  # empty while it goes, then ok, no change, stopped or failed: <why>
    stack_kind: str = ""
    new_kind: str = ""
    analysis: str = ""
    title: str = ""
    text: str = ""
    jar: str = ""
    sandbox_url: str = ""
    database: str = ""
    feature_log: str = ""
    pr_url: str = ""

    @property
    def path(self) -> Path:
        return RUNS_DIR / self.id

    @property
    def finished(self) -> bool:
        return self.outcome in {"ok", "no change"}

    def save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "state.json").write_text(self.model_dump_json(indent=2) + "\n")

    @classmethod
    def load(cls, run: str) -> RunState:
        f = RUNS_DIR / run / "state.json"
        if not f.is_file():
            raise FileNotFoundError(f"no run {run} in {RUNS_DIR}")
        return cls.model_validate_json(f.read_text())

    @classmethod
    def latest(cls) -> RunState | None:
        runs = sorted(p.name for p in RUNS_DIR.glob("*") if RUN_ID.match(p.name) and (p / "state.json").is_file())
        return cls.load(runs[-1]) if runs else None


def pack(paths: dict[str, bytes | Path]) -> bytes:
    """A .tar.gz, in memory, of these files (bytes) and of these files or directories on this computer (Path)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in paths.items():
            if isinstance(data, Path):
                tar.add(data, name)
                continue
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@dataclass
class Agent:
    settings: Settings
    nevia: Nevia
    poll: float = 10.0
    show: Callable[[str], None] = console.log
    stack: Stack = field(init=False)
    tunnels: Tunnels = field(init=False)

    def __post_init__(self) -> None:
        self.stack = Stack(self.settings, self.nevia, self.poll)
        self.tunnels = Tunnels(self.nevia, lifetime=ANALYSIS_LINK_SECONDS)

    @property
    def name(self) -> str:
        return self.settings.agent.computer

    @property
    def dir(self) -> str:
        return self.settings.agent.dir

    def computer(self) -> Computer:
        return Computer(self.name, self.nevia, self.poll)

    def run_dir(self, run: str) -> str:
        """The run's directory on the agent computer, under its home."""
        return f"{self.dir}/runs/{run}"

    def _step(self, job: str, *args: str, env: dict[str, str], timeout: str = "") -> str:
        """A step that needs a secret, as a job (`agent acc remote step`), with it in `env`; its log's end."""
        cmd = agent_command("acc", "step", *args)
        env = env | ({"TZ": tz} if (tz := os.environ.get("TZ")) else {})  # its log in your machine's time zone
        return self.computer().run_job(job, f"timeout {timeout} {cmd}" if timeout else cmd, env=env)

    def _token(self) -> dict[str, str]:
        s = self.settings
        return {"GITHUB_TOKEN": secret(s.secret(s.repo.token_secret))}

    # ------------------------------------------------------------------ setup

    def setup(self) -> None:
        """On the agent computer: its tools, JDK, Maven, psql, Claude Code for its own Unix user, mongo2pg, and the
        stack's passwords. The app's stack is a run's: each run recreates it from its clone. Safe to repeat."""
        Builder(self.settings.mongo2pg).install(steps.HOME / self.dir / "bin/mongo2pg")
        self.stack.passwords()
        c = self.settings.claude
        self.upload_tools(self.computer())
        steps.setup(self.settings)
        if not c.gateway_url:
            console.log(f"log Claude Code in once, if not done: nevia shell {self.name}, then "
                        f"sudo -iu {c.user} claude, and /login")  # fmt: skip

    def upload_tools(self, pc: Computer) -> None:
        """lib.sh, and the tools read-only, replaced as a whole so that tools an earlier version had go too."""
        t = f"~/{self.dir}/tools"
        pc.sh(f"chmod -R u+w {t} 2>/dev/null; rm -rf {t} && mkdir -p {t}")
        pc.put_lib()
        for name in TOOLS:
            pc.put(name, f"{self.dir}/tools/{name}", "444")
        pc.upload(bundled("core", "scripts/common.jq"), f"{self.dir}/tools/common.jq", "444")

    # ------------------------------------------------------------------ the stack

    @staticmethod
    def _sources(src: str, ca: str = "") -> bytes:
        """pom.xml and src/ of the clone in `src` (on this computer, under the home), as a .tar.gz, with the CA if
        given."""
        repo = steps.HOME / src
        return pack({"pom.xml": repo / "pom.xml", "src": repo / "src", **({CA_FILE: ca.encode()} if ca else {})})

    @staticmethod
    def _mongo_init(src: str) -> bytes | None:
        """local/mongo-init/*.js of the clone, as a .tar.gz, or nothing when it has none."""
        scripts = sorted((steps.HOME / src / "local/mongo-init").glob("*.js"))
        return pack({f.name: f for f in scripts}) if scripts else None

    def _pg_init(self, src: str, secret_name: str, job: str) -> None:
        self._step(job, "pg-init", src, env={"DATABASE_URL": secret(secret_name)})

    def deploy_stack(self, src: str, kind: str) -> str:
        """The app of the clone in `src` (on the agent computer) on its computer, with the database its
        database.kind names. The app and MongoDB computers are deleted first (their data with them), so the
        stack is never an older version, and starts from the seed data. Returns the app's URL."""
        s = self.settings
        if kind not in {"mongo", "postgres"}:
            raise RuntimeError(f"database.kind must be mongo or postgres, got {kind!r}")
        for name in (s.app.computer, s.mongo.computer):
            if self.nevia.computers.exists(name):
                console.log(f"deleting computer {name}")
                self.nevia.computers.delete(name)
        if kind == "mongo":
            self.stack.mongo_up(s.mongo.computer, init=self._mongo_init(src))
            env, ca = self.stack.mongo_env(s.mongo.computer), ""
        else:
            sec = s.secret(s.postgres.secret)
            created, ca = self.stack.postgres_up(s.postgres.name, sec)
            if created:
                self._pg_init(src, sec, "pg-init-base")
            env = self.stack.postgres_env(sec)
        url = self.stack.app_up(s.app.computer, kind, env, self._sources(src, ca))
        console.log(f"{s.app.computer}: {url}")
        return url

    # ------------------------------------------------------------------ a run

    def unfinished(self) -> RunState | None:
        st = RunState.latest()
        return st if st and not st.finished else None

    def start(self, kind: RunKind, task: str = "") -> RunState:
        if kind == "task" and not task.strip():
            raise ValueError("what to build? agent acc run task '<the feature>' (or --file)")
        st = RunState(id=datetime.now(UTC).strftime("%y%m%d%H%M%S"), kind=kind, task=task,
                      started=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"))  # fmt: skip
        st.save()
        console.log(f"run {st.id}: {kind} of {self.settings.repo.github} ({self.settings.repo.base_branch})")
        return st

    def redo(self, st: RunState, step: str) -> None:
        """Forget that `step` and the ones after it were done, so that `proceed` does them again."""
        order = STEPS[st.kind]
        if step not in order:
            raise ValueError(f"no step {step!r} in a {st.kind} run: {', '.join(order)}")
        st.done = [s for s in st.done if order.index(s) < order.index(step)]
        st.save()

    def proceed(self, st: RunState) -> RunState:
        """The run's steps not done yet, in order; the report at the end, failed or not."""
        pc = self.computer()
        pc.awake()
        self.upload_tools(pc)
        st.outcome = ""
        st.save()
        try:
            for step in STEPS[st.kind]:
                if step in st.done:
                    continue
                console.rule(f"{st.id}: {step}")
                t0 = time.monotonic()
                try:
                    getattr(self, "_" + step.replace("-", "_"))(st)
                finally:
                    st.durations[step] = st.durations.get(step, 0) + time.monotonic() - t0
                st.done.append(step)
                st.save()
                if st.outcome == "no change":
                    break
            st.outcome = st.outcome or "ok"
        except KeyboardInterrupt:  # only the following stops: what runs on the agent computer goes on
            raise
        except Exception as err:
            st.outcome = f"failed: {str(err).splitlines()[0] if str(err) else type(err).__name__}"
            raise
        finally:
            st.save()
            self.write_report(st)
        return st

    def _kind(self, st: RunState) -> str:
        props = f"{self.run_dir(st.id)}/repo/src/main/resources/application.properties"
        return steps.db_kind(self.computer().download(props).decode())

    def _clone(self, st: RunState) -> None:
        self._step(f"clone-{st.id}", "clone", st.id, env=self._token())
        st.stack_kind = self._kind(st)

    def _stack(self, st: RunState) -> None:
        self.deploy_stack(f"{self.run_dir(st.id)}/repo", st.stack_kind)

    def _dev_db(self, run: str) -> tuple[str, str]:
        """Claude's own empty Postgres for a migration, `<app>-db-<run>`, and the secret of its URI."""
        s = self.settings
        return f"{s.app.computer}-db-{run}", f"{s.secret(s.postgres.secret)}_DEV_{run}"

    def _dev_postgres(self, run: str) -> str:
        """Claude's Postgres, made if need be, its CA in the run's directory; returns the secret of its URI."""
        db, sec = self._dev_db(run)
        _, ca = self.stack.postgres_up(db, sec)
        self.computer().upload(ca.encode(), f"{self.run_dir(run)}/dev-ca.pem", "644")
        return sec

    def _analysis(self, st: RunState) -> None:
        """mongo2pg init and infer on the agent computer, on the stack's MongoDB through a link made for it."""
        m, rd = self.settings.mongo, self.run_dir(st.id)
        sec = self._dev_postgres(st.id)
        link = self.tunnels.link(self.name, m.computer, 27017)
        try:
            uri = (f"mongodb://{m.user}:{secret(self.settings.secret(m.password_secret))}@127.0.0.1:{link.local_port}/"
                   "?authSource=admin&directConnection=true")  # fmt: skip
            out = self._step(f"analysis-{st.id}", "analyse", st.id, env={"MONGO_URI": uri, "DATABASE_URL": secret(sec)})
        finally:
            self.tunnels.unlink(m.computer, link.id)
        console.log(out.strip().splitlines()[-1])
        st.analysis = self.computer().sh(f"cat ~/{rd}/analysis/summary.md")

    def _claude(self, st: RunState) -> None:
        """Claude Code on the change, followed as it goes. A run taken up again follows the job still going,
        takes the result of one that ended with one, and starts Claude again otherwise."""
        pc, rd, job = self.computer(), self.run_dir(st.id), f"claude-{st.id}"
        has_result = pc.sh(f"test -s ~/{rd}/result.json && echo yes || true").strip() == "yes"
        if not pc.job_running(job) and not has_result:
            home = pc.sh("echo $HOME").strip()
            env = claude_env(self.settings)
            if st.kind == "migrate":
                env |= {"DATABASE_URL": secret(self._dev_postgres(st.id)), "PG_CA_FILE": f"{home}/{rd}/dev-ca.pem"}
            text = prompt(st.kind, f"{home}/{rd}", st.task, repo=self.settings.repo.github,
                          branch=self.settings.repo.base_branch)  # fmt: skip
            pc.upload(text.encode(), f"{rd}/prompt.md")
            # Claude runs as its own Unix user, with the clone and its work directory lent to it for the time:
            # the key file and this orchestrator's env are another user's, out of its reach.
            pc.sh(f"mkdir -p ~/{rd}/work && sudo -n chown -R {self.settings.claude.user}: ~/{rd}/repo ~/{rd}/work")
            cmd = agent_command("acc", "step", "claude", st.id)
            pc.start_job(job, f"timeout {self.settings.agent.hours}h {cmd}", env=env)
        seen = [0]
        try:
            pc.wait_job(job, on_poll=lambda: self._show_activity(st.id, seen))
        finally:
            pc.sh(f"sudo -n chown -R $(id -u):$(id -g) ~/{rd}/repo ~/{rd}/work")  # back, for the next steps
            self._show_activity(st.id, seen)
            self._fetch_result(st)
            if st.kind == "migrate":
                self._drop_dev_db(st.id)
        if not (st.path / "result.json").is_file():
            raise RuntimeError("Claude Code ended without a result: `agent acc run resume` starts it again")
        result = json.loads((st.path / "result.json").read_text())
        st.title, st.text = pr.proposal(result)
        self.show(f"Claude's proposal: {st.title}")
        if not pc.sh(f"git -C ~/{rd}/repo status --porcelain").strip():
            self.show("no change proposed: no sandbox, no pull request")
            st.outcome = "no change"
            return
        if st.kind == "task" and not pc.sh(f"test -s ~/{rd}/{CHECK} && echo yes || true").strip():
            raise RuntimeError(f"Claude wrote no feature check ({CHECK}): no sandbox, no pull request")

    def activity(self, run: str, start: int = 0) -> list[str]:
        """Claude's lines in the run's log (tools/render.jq's), from line `start` on."""
        log = f"~/{self.run_dir(run)}/logs/claude.log"
        out = self.computer().sh(f"test -f {log} && tail -n +{start + 1} {log} || true")
        return [redact(line) for line in out.splitlines()]

    def _show_activity(self, run: str, seen: list[int]) -> None:
        lines = self.activity(run, seen[0])
        seen[0] += len(lines)
        for line in lines:
            self.show(line)

    def _fetch_result(self, st: RunState) -> None:
        f = f"{self.run_dir(st.id)}/result.json"
        if self.computer().sh(f"test -s ~/{f} && echo yes || true").strip():
            (st.path / "result.json").write_bytes(self.computer().download(f))

    def _drop_dev_db(self, run: str) -> None:
        db, sec = self._dev_db(run)
        if self.nevia.databases.exists(db):
            console.log(f"deleting {db}, Claude's Postgres")
            self.nevia.databases.delete(db)
        if any(s["name"] == sec for s in self.nevia.secrets.list()):
            self.nevia.secrets.delete(sec)

    def _build(self, st: RunState) -> None:
        st.jar = str(steps.build(self.settings, st.id).relative_to(steps.HOME))

    def _sandbox(self, st: RunState) -> None:
        """A checkpoint of the app restored as `<app>-<run>`, on a database of its own: a branch of the stack's
        when the change keeps its kind (a fork of its data), a new one with the seed data otherwise. The jar
        built on the agent goes there and must answer."""
        s, rd = self.settings, self.run_dir(st.id)
        app, run = s.app.computer, st.id
        st.new_kind = self._kind(st)
        kind: Kind = "mongo" if st.new_kind == "mongo" else "postgres"
        if st.new_kind not in {"mongo", "postgres"}:
            raise RuntimeError(f"database.kind must be mongo or postgres after the change, got {st.new_kind!r}")
        checkpoint, sandbox = f"acc-{run}", sandbox_name(app, run)
        if not any(c["name"] == checkpoint and c["state"] == "ready" for c in self.nevia.checkpoints.list(app)):
            console.log(f"checkpoint {checkpoint} of {app}")
            self.nevia.checkpoints.create(app, checkpoint)
        if not self.nevia.computers.exists(sandbox):
            console.log(f"restoring {checkpoint} as {sandbox}")
            self.nevia.checkpoints.restore(app, checkpoint, sandbox)
        host = re.sub(r"^https://|[:/].*$", "", Computer(app, self.nevia).url)
        self.stack.wait_dns(sandbox, host)
        files: dict[str, bytes | Path] = {"app.jar": steps.HOME / st.jar}  # the jar built here
        if kind == "mongo":
            fork = sandbox_name(s.mongo.computer, run)
            self.stack.mongo_up(fork, branch_of=s.mongo.computer)
            env = self.stack.mongo_env(fork)
            st.database = f"`{fork}`, a branch of `{s.mongo.computer}`: a fork of its data"
        else:
            db, sec = sandbox_name(s.postgres.name, run), f"{s.secret(s.postgres.secret)}_{run}"
            branch_of = s.postgres.name if st.stack_kind == "postgres" else None
            created, ca = self.stack.postgres_up(db, sec, branch_of=branch_of)
            if created:
                self._pg_init(f"{rd}/repo", sec, f"pg-init-{run}")
                st.database = f"`{db}`, a new Postgres with local/postgres-init"
            else:
                st.database = f"`{db}`, a branch of `{s.postgres.name}`: a fork of its data"
            env = self.stack.postgres_env(sec)
            files[CA_FILE] = ca.encode()
        st.sandbox_url = self.stack.app_up(sandbox, kind, env, pack(files))
        self.show(f"sandbox: {st.sandbox_url}")

    def _feature_check(self, st: RunState) -> None:
        """Claude's script on the sandbox, with nothing but PATH and HOME: no more than Claude itself could do."""
        try:
            st.feature_log = steps.feature_check(self.settings, st.id, st.sandbox_url)
        except RuntimeError as err:
            raise RuntimeError(f"the feature check fails on the sandbox: no pull request\n{err}") from None

    def _pr(self, st: RunState) -> None:
        s, pc, rd = self.settings, self.computer(), self.run_dir(st.id)
        v = pr.Validation(
            stack=s.app.computer, base_branch=s.repo.base_branch, sandbox=sandbox_name(s.app.computer, st.id),
            checkpoint=f"acc-{st.id}", kind=st.new_kind, database=st.database, url=st.sandbox_url,
            analysis=st.analysis,
            feature_check=pc.sh(f"cat ~/{rd}/{CHECK}") if st.kind == "task" else "", feature_log=st.feature_log,
        )  # fmt: skip
        text = f"{st.title}\n\n{pr.body(st.text, v, st.id)}"
        (st.path / "pr.md").write_text(text)
        pc.upload(text.encode(), f"{rd}/pr.md")
        out = self._step(f"pr-{st.id}", "pr", st.id, env=self._token())
        st.pr_url = out.strip().splitlines()[-1]
        self.show(f"pull request: {st.pr_url}")

    # ------------------------------------------------------------------ report, status, stop

    def left(self, run: str) -> list[str]:
        """What the run left on Nevia."""
        d = self.doomed(run)
        return ([f"computer `{c}`" for c in d.computers] + [f"checkpoint `{k}` of `{c}`" for c, k in d.checkpoints]
                + [f"database `{x}`" for x in d.databases] + [f"secret `{x}`" for x in d.secrets])  # fmt: skip

    def write_report(self, st: RunState) -> Path:
        result = None
        if (f := st.path / "result.json").is_file():
            result = json.loads(f.read_text())
        try:
            left = self.left(st.id)
        except NeviaError:
            left = ["(could not list them)"]
        r = pr.Report(
            run=st.id, kind=st.kind, repo=self.settings.repo.github, base_branch=self.settings.repo.base_branch,
            started=st.started, outcome=st.outcome or "going", steps=list(st.durations.items()), pr_url=st.pr_url,
            sandbox_url=st.sandbox_url, result=result, left=left,
        )  # fmt: skip
        path = st.path / "report.md"
        path.write_text(pr.report(r))
        cost = f", Claude ${float(result.get('total_cost_usd') or 0):.2f}" if result else ""
        console.log(f"report: {path.relative_to(BUILD_DIR.parent)} ({st.outcome or 'going'}{cost})")
        return path

    def claude_running(self, run: str) -> bool:
        return self.computer().job_running(f"claude-{run}")

    def stop(self) -> None:
        """Stop Claude and the jobs of the run going on the agent computer; what it made stays (`clean`)."""
        self.computer().sh(
            # [r]: the patterns must not match the shell running them. The jobs' wrappers still write the
            # exit code a `run` following them waits for.
            "pkill -f 'core acc remote [r]un' || true; pkill -f 'core acc remote [s]tep' || true; "
            f"sudo -n pkill -u {self.settings.claude.user} || true"
        )
        if st := self.unfinished():
            st.outcome = "stopped"
            st.save()
        console.log(f"{self.name}: stopped")

    # ------------------------------------------------------------------ clean, destroy

    def _secrets_now(self) -> set[str]:
        return {s["name"] for s in self.nevia.secrets.list()}

    def doomed(self, run: str | None = None) -> Doomed:
        """What a run left (all runs' when None): sandbox computers, the checkpoint, databases, secrets, files."""
        s = self.settings
        app, mongo, pg = s.app.computer, s.mongo.computer, s.postgres.name

        def mine(name: str, base: str) -> bool:
            m = SUFFIXED.search(name)
            return bool(m) and name == f"{base}-{m[1]}" and run in {None, m[1]}

        computers = [c["name"] for c in self.nevia.computers.list()]
        sandboxes = sorted(c for c in computers if mine(c, app)) + sorted(c for c in computers if mine(c, mongo))
        checkpoints = []
        if app in computers:
            for c in self.nevia.checkpoints.list(app):
                if (m := CHECKPOINT.fullmatch(c["name"] or "")) and run in {None, m[1]}:
                    checkpoints.append((app, m[0]))
        dbs = [d["name"] for d in self.nevia.databases.list()]
        databases = sorted(d for d in dbs if mine(d, pg) or mine(d, f"{app}-db"))
        pattern = re.compile(rf"^{re.escape(s.secret(s.postgres.secret))}_(DEV_)?(\d{{12}})$")
        secrets = sorted(n for n in self._secrets_now() if (m := pattern.match(n)) and run in {None, m[2]})
        dirs = [self.run_dir(run)] if run else [f"{self.dir}/runs"]
        jobs = f"*-{run}.*" if run else "*-[0-9]*.*"
        return Doomed(sandboxes, sorted(checkpoints), databases, secrets, dirs=dirs, jobs=jobs)

    def doomed_all(self) -> Doomed:
        """What the agent deletes in `destroy`: every run's, then the stack and the secrets it made. The agent
        computer, its principal and the secrets from `.env` are your machine's to delete (`Bootstrap`)."""
        s, runs = self.settings, self.doomed()
        fixed = [s.app.computer, s.mongo.computer]
        computers = [*runs.computers, *(c for c in fixed if self.nevia.computers.exists(c))]
        databases = [*runs.databases, *([s.postgres.name] if self.nevia.databases.exists(s.postgres.name) else [])]
        fields = (s.postgres.secret, s.mongo.password_secret, s.chisel.password_secret)
        names = [s.secret(f) for f in fields]
        have = self._secrets_now()
        return Doomed(computers, runs.checkpoints, databases, [*runs.secrets, *(n for n in names if n in have)])

    def delete(self, d: Doomed) -> None:
        """Delete what `d` lists, in its order: the sandboxes before the checkpoint they come from."""
        nv = self.nevia
        sandboxes = [c for c in d.computers if SUFFIXED.search(c)]
        for name in sandboxes:
            nv.computers.delete(name)
            console.log(f"deleted computer {name}")
        for computer, checkpoint in d.checkpoints:
            nv.checkpoints.delete(computer, checkpoint)
            console.log(f"deleted checkpoint {checkpoint} of {computer}")
        if d.dirs and nv.computers.exists(self.name):
            self.computer().awake()
            paths = " ".join(f"~/{shlex.quote(p)}" for p in d.dirs)
            self.computer().sh(f"chmod -R u+w {paths} 2>/dev/null; rm -rf {paths} {JOBS_DIR}/{d.jobs}")
            console.log(f"deleted {', '.join(d.dirs)} on {self.name}")
        for name in (c for c in d.computers if c not in sandboxes):
            nv.computers.delete(name)
            console.log(f"deleted computer {name}")
        for name in d.databases:
            nv.databases.delete(name)
            console.log(f"deleted database {name}")
        for name in d.secrets:
            nv.secrets.delete(name)
            console.log(f"deleted secret {name}")


# ---------------------------------------------------------------------- the agent computer's commands
# Your machine's commands (acc.local) run these there, through `agent acc remote`.

remote = typer.Typer(no_args_is_help=True, hidden=True, help="On the agent computer: what the commands do there.")
out = rich.console.Console(soft_wrap=True)
LATEST = 15  # Claude's lines `status` shows

YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Don't ask first.")]
Mode = Literal["migrate", "task", "resume"]
ModeArg = Annotated[
    Mode, typer.Argument(help="migrate, task, or resume: the last run taken up (or followed, if it is going).")
]
FromOpt = Annotated[
    str | None, typer.Option("--from", help="With resume: redo this step and the ones after (e.g. sandbox).")
]


def _here() -> Agent:
    s = config.load()
    return Agent(s, Nevia(s.nevia.workspace), show=lambda line: out.print(line, markup=False, highlight=False))


@remote.command("setup")
def remote_setup() -> None:
    _here().setup()


@remote.command("run")
def remote_run(
    kind: ModeArg,
    file: Annotated[Path | None, typer.Option("--file", help="The request, for `task`.")] = None,
    step: FromOpt = None,
) -> None:
    """A run's steps: a new one, or the last one taken up."""
    ag = _here()
    going = ag.unfinished()
    if kind == "resume":
        if going is None:
            raise typer.BadParameter("no run to take up: agent acc run migrate, or agent acc run task '<feature>'")
        st = going
        if step:
            ag.redo(st, step)
        out.print(f"run {st.id} ({st.kind}): taking it up after {', '.join(st.done) or 'nothing'}")
    else:
        st = ag.start(kind, file.expanduser().read_text() if file else "")
    ag.proceed(st)


@remote.command("status")
def remote_status() -> None:
    ag = _here()
    st = RunState.latest()
    if st is None:
        out.print("no run yet")
    else:
        out.print(
            f"run {st.id} ({st.kind}, {st.started}): {st.outcome or 'going'}; done: {', '.join(st.done) or 'nothing'}"
        )
        for label, value in [("sandbox", st.sandbox_url), ("pull request", st.pr_url)]:
            if value:
                out.print(f"{label}: {value}")
        if "claude" not in st.done and (acts := events(ag.activity(st.id))):
            out.rule("Claude's latest actions" + (" (running)" if ag.claude_running(st.id) else ""))
            for a in acts[-LATEST:]:
                out.print(f"{a.time} {a.text}", markup=False, highlight=False, style="red" if a.failed else None)
    out.print(f"sandboxes: {', '.join(ag.doomed().computers) or 'none'}")


@remote.command("stop")
def remote_stop() -> None:
    _here().stop()


@remote.command("clean")
def remote_clean(run: Annotated[str | None, typer.Option()] = None, yes: YesOpt = False) -> None:
    """Without --yes, what it would delete, as JSON."""
    ag = _here()
    if (st := ag.unfinished()) and ag.claude_running(st.id) and run in {None, st.id}:
        raise typer.BadParameter(f"run {st.id} is going: agent acc stop first")
    d = ag.doomed(run)
    if yes:
        ag.delete(d)
    else:
        print(d.to_json())  # noqa: T201 - read by your machine


@remote.command("destroy")
def remote_destroy(yes: YesOpt = False) -> None:
    """Without --yes, what it would delete, as JSON."""
    ag = _here()
    if yes:
        ag.delete(ag.doomed_all())
    else:
        print(ag.doomed_all().to_json())  # noqa: T201 - read by your machine


@remote.command("step")
def remote_step(name: str, arg: str) -> None:
    """A step that needs a secret, which this job's env holds: clone, analyse, claude, pr (`arg`: the run), or
    pg-init (`arg`: the clone, under the home)."""
    s = config.load()
    match name:
        case "clone":
            steps.clone(s, arg)
        case "analyse":
            steps.analyse(s, arg)
        case "claude":
            steps.claude(s, arg)
        case "pr":
            print(steps.open_pr(s, arg))  # noqa: T201 - read by the orchestrator
        case "pg-init":
            steps.pg_init(steps.HOME / arg)
        case _:
            raise typer.BadParameter(f"no step {name!r}")
