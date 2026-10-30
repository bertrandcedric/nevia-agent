"""The agent: an AI (`[ai] engine`) on its own Nevia computer changes the app, and the change goes to a pull
request once a sandbox has passed.

This runs on the agent computer: it makes the databases, the checkpoint and the sandbox with the nevia CLI, and does
the work itself (`acc.steps`); a step that needs a secret runs as a job (`agent acc remote step ...`), with the
secret in its env. Each step is recorded once done, in build/acc-runs/<run>/state.json, so `agent acc run resume`
takes a stopped or failed run up from where it stopped.
"""

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from acc import pr, steps
from acc.cleanup import Cleanup
from acc.computer import Computer, redact, secret
from acc.config import AI_USER, Settings
from acc.run import RUN_JOB, STEPS, FromOpt, Kind, ModeArg, RunKind, RunState, YesOpt, run_dir_of
from acc.stack import CA_FILE, RunNames, Stack, pack
from acc.steps import CHECK
from core.activity import events
from core.agent import ai, prompts
from core.agent.build import Builder
from core.agent.frame import Here, remote_app
from core.computer import MONGO_PORT, agent_command, carried
from core.config import BUILD_DIR, new_run_id
from core.doomed import listed_or_deleted
from core.nevia import NeviaError
from core.results import REPORT, RESULT, cost_suffix
from core.shell import console, out

ANALYSIS_LINK_SECONDS = 3600  # the agent's link to MongoDB, for mongo2pg infer only
BASE_PG_INIT_JOB = "pg-init-base"  # the stack's Postgres made from scratch; a run's other jobs are `<step>-<run>`


def prompt(kind: RunKind, run_dir: str, task: str = "", *, repo: str, branch: str) -> str:
    """The AI's task: the migration's, or the feature asked for wrapped in the task's. `{{...}}` are replaced as
    text, the request last, so that its own braces stay as they are."""
    text = prompts.prompt("acc", "MIGRATE.md" if kind == "migrate" else "FEATURE.md")
    for key, value in {"REPO": repo, "BRANCH": branch, "RUN_DIR": run_dir, "CHECK": f"{run_dir}/{CHECK}"}.items():
        text = text.replace("{{" + key + "}}", value)
    return text.replace("{{TASK}}", task.strip())


def ai_env(s: Settings) -> dict[str, str]:
    """What the AI's step gets in its env: the gateway and its key as a secret template (`core.agent.ai`); its
    other settings come from agent.toml."""
    return carried(ai.env(s.ai, s.secret(s.ai.gateway_secret)))  # the log's times as your clock reads them


def _filled(path: Path) -> bool:
    """Whether `path` is there and not empty (`test -s`)."""
    return path.exists() and path.stat().st_size > 0


@dataclass
class Agent(Here[Settings]):
    show: Callable[[str], None] = console.log
    stack: Stack = field(init=False)
    cleanup: Cleanup = field(init=False)
    dispatch: dict[str, Callable[[RunState], None]] = field(init=False)  # what does each of STEPS, built once

    def __post_init__(self) -> None:
        super().__post_init__()
        self.stack = Stack(self.settings, self.nevia, self.poll)
        self.cleanup = Cleanup(self.settings, self.nevia, self.poll)
        self.dispatch = {
            "clone": self._clone, "stack": self._stack, "analysis": self._analysis, "ai": self._ai,
            "build": self._build, "sandbox": self._sandbox, "feature-check": self._feature_check, "pr": self._pr,
        }  # fmt: skip

    def link_lifetime(self) -> int:
        return ANALYSIS_LINK_SECONDS

    def computer(self) -> Computer:
        return Computer(self.name, self.nevia, self.poll)

    def run_dir(self, run: str) -> str:
        """The run's directory on the agent computer, under its home."""
        return run_dir_of(self.dir, run)

    def _step(self, job: str, *args: str, env: dict[str, str], timeout: str = "") -> str:
        """A step that needs a secret, as a job (`agent acc remote step`), with it in `env`; its log's end."""
        cmd = agent_command("acc", "step", *args)
        return self.computer().run_job(job, f"timeout {timeout} {cmd}" if timeout else cmd, env=carried(env))

    def _token(self) -> dict[str, str]:
        s = self.settings
        return {"GITHUB_TOKEN": secret(s.secret(s.repo.token_secret))}

    # ------------------------------------------------------------------ setup

    def setup(self) -> None:
        """On the agent computer: its tools, JDK, Maven, psql, the AI for its own Unix user, mongo2pg, and the
        stack's passwords. The app's stack is a run's: each run recreates it from its clone. Safe to repeat."""
        Builder(self.settings.mongo2pg).install(steps.HOME / self.dir / "bin/mongo2pg")
        self.stack.passwords()
        self.upload_tools(self.computer())
        steps.setup(self.settings)

    def upload_tools(self, pc: Computer) -> None:
        """lib.sh, and the AI's tools read-only (`Computer.put_tools`)."""
        pc.put_tools(f"{self.dir}/tools", ai.tools())

    # ------------------------------------------------------------------ a run

    def unfinished(self) -> RunState | None:
        st = RunState.latest()
        return st if st and not st.finished else None

    def start(self, kind: RunKind, task: str = "") -> RunState:
        if kind == "task" and not task.strip():
            raise ValueError("what to build? agent acc run task '<the feature>' (or --file)")
        st = RunState(id=new_run_id(), kind=kind, task=task,
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
                    self.dispatch[step](st)
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

    def _kind(self, st: RunState) -> Kind:
        return steps.db_kind(
            (steps.run_dir(self.settings, st.id) / "repo/src/main/resources/application.properties").read_text()
        )

    def _clone(self, st: RunState) -> None:
        self._step(f"clone-{st.id}", "clone", st.id, env=self._token())
        st.stack_kind = self._kind(st)

    def _pg_init(self, src: str, secret_name: str, job: str) -> None:
        self._step(job, "pg-init", src, env={"DATABASE_URL": secret(secret_name)})

    def _stack(self, st: RunState) -> None:
        """The app of the run's clone on its computer (`Stack.deploy`), with the database its database.kind
        names; the earlier runs' sandboxes go first."""
        src = f"{self.run_dir(st.id)}/repo"
        repo, kind = steps.HOME / src, st.stack_kind or self._kind(st)
        self.stack.deploy(repo, kind, lambda sec: self._pg_init(src, sec, BASE_PG_INIT_JOB), st.id)

    def names(self, run: str) -> RunNames:
        """The Nevia names of what the run makes."""
        return RunNames(self.settings, run)

    def _dev_postgres(self, run: str) -> str:
        """The AI's Postgres, made if need be, its CA in the run's directory; returns the secret of its URI."""
        n = self.names(run)
        _, ca = self.stack.postgres_up(n.dev_db, n.dev_secret)
        self.computer().upload(ca.encode(), f"{self.run_dir(run)}/dev-ca.pem", "644")
        return n.dev_secret

    def _analysis(self, st: RunState) -> None:
        """mongo2pg init and infer on the agent computer, on the stack's MongoDB through a link made for it."""
        m = self.settings.mongo
        sec = self._dev_postgres(st.id)
        link = self.tunnels.link(self.name, m.computer, MONGO_PORT)
        try:
            uri = self.stack.mongo_uri(link.local_port)
            out = self._step(f"analysis-{st.id}", "analyse", st.id, env={"MONGO_URI": uri, "DATABASE_URL": secret(sec)})
        finally:
            self.tunnels.unlink(m.computer, link.id)
        console.log(out.strip().splitlines()[-1])
        st.analysis = (steps.run_dir(self.settings, st.id) / "analysis/summary.md").read_text()

    def _ai(self, st: RunState) -> None:
        """The AI on the change, followed as it goes. A run taken up again follows the job still going, takes the
        result of one that ended with one, and starts the AI again otherwise."""
        job = f"ai-{st.id}"
        if not self.computer().job_running(job) and not _filled(steps.run_dir(self.settings, st.id) / RESULT):
            self._start_ai(st, job)
        self._follow_ai(st, job)
        self._proposal(st)

    def _start_ai(self, st: RunState, job: str) -> None:
        """The AI's job, on its prompt, with the gateway key (and for a migration its own Postgres) in its env."""
        pc, rd, path = self.computer(), self.run_dir(st.id), steps.run_dir(self.settings, st.id)
        env = ai_env(self.settings)
        if st.kind == "migrate":
            env |= {"DATABASE_URL": secret(self._dev_postgres(st.id)), "PG_CA_FILE": f"{path}/dev-ca.pem"}
        text = prompt(st.kind, str(path), st.task, repo=self.settings.repo.github,
                      branch=self.settings.repo.base_branch)  # fmt: skip
        pc.upload(text.encode(), f"{rd}/prompt.md")
        # The AI runs as its own Unix user, with the clone and its work directory lent to it for the time:
        # the key file and this orchestrator's env are another user's, out of its reach.
        pc.sh(f"mkdir -p ~/{rd}/work && sudo -n chown -R {AI_USER}: ~/{rd}/repo ~/{rd}/work")
        cmd = agent_command("acc", "step", "ai", st.id)
        pc.start_job(job, f"timeout {self.settings.agent.hours}h {cmd}", env=env)

    def _follow_ai(self, st: RunState, job: str) -> None:
        """The AI's lines as they come, until its job ends; then the clone back, its result into the run's own
        directory, and a migration's Postgres deleted."""
        pc, rd = self.computer(), self.run_dir(st.id)
        seen = [0]
        try:
            pc.wait_job(job, on_poll=lambda: self._show_activity(st.id, seen))
        finally:
            pc.sh(f"sudo -n chown -R $(id -u):$(id -g) ~/{rd}/repo ~/{rd}/work")  # back, for the next steps
            self._show_activity(st.id, seen)
            self._fetch_result(st)
            if st.kind == "migrate":
                self._drop_dev_db(st.id)

    def _proposal(self, st: RunState) -> None:
        """The AI's result read into the pull request's title and text; no change, and the run ends here."""
        if not (st.path / RESULT).is_file():
            raise RuntimeError(
                f"{self.settings.ai.name} ended without a result: `agent acc run resume` starts it again"
            )
        result = json.loads((st.path / RESULT).read_text())
        st.title, st.text = pr.proposal(result, self.settings.ai.name)
        self.show(f"the AI's proposal: {st.title}")
        if not self.computer().sh(f"git -C ~/{self.run_dir(st.id)}/repo status --porcelain").strip():
            self.show("no change proposed: no sandbox, no pull request")
            st.outcome = "no change"
            return
        if st.kind == "task" and not _filled(steps.run_dir(self.settings, st.id) / CHECK):
            raise RuntimeError(f"the AI wrote no feature check ({CHECK}): no sandbox, no pull request")

    def activity(self, run: str, start: int = 0) -> list[str]:
        """The AI's lines in the run's log (tools/render-<engine>.jq's), from line `start` on."""
        log = ai.log(steps.run_dir(self.settings, run), 1)
        lines = log.read_bytes().decode(errors="replace").splitlines() if log.is_file() else []  # being written
        return [redact(line) for line in lines[start:]]

    def _show_activity(self, run: str, seen: list[int]) -> None:
        lines = self.activity(run, seen[0])
        seen[0] += len(lines)
        for line in lines:
            self.show(line)

    def _fetch_result(self, st: RunState) -> None:
        if _filled(f := steps.run_dir(self.settings, st.id) / RESULT):
            shutil.copyfile(f, st.path / RESULT)

    def _drop_dev_db(self, run: str) -> None:
        n = self.names(run)
        db, sec = n.dev_db, n.dev_secret
        if self.nevia.databases.exists(db):
            console.log(f"deleting {db}, the AI's Postgres")
            self.nevia.databases.delete(db)
        if self.nevia.secrets.exists(sec):
            self.nevia.secrets.delete(sec)

    def _build(self, st: RunState) -> None:
        st.jar = str(steps.build(self.settings, st.id).relative_to(steps.HOME))

    def _sandbox(self, st: RunState) -> None:
        """A checkpoint of the app restored as `<app>-<run>` (`Stack.sandbox`), on a database of its own: a branch
        of the stack's when the change keeps its kind (a fork of its data), a new one with the seed data
        otherwise. The jar built on the agent goes there and must answer."""
        s, rd, n = self.settings, self.run_dir(st.id), self.names(st.id)
        kind = st.new_kind = self._kind(st)
        sandbox = self.stack.sandbox(st.id)
        files: dict[str, bytes | Path] = {"app.jar": steps.HOME / st.jar}  # the jar built here
        if kind == "mongo":
            fork = n.mongo_fork
            self.stack.mongo_up(fork, branch_of=s.mongo.computer)
            env = self.stack.mongo_env(fork)
            st.database = f"`{fork}`, a branch of `{s.mongo.computer}`: a fork of its data"
        else:
            db, sec = n.pg, n.pg_secret
            branch_of = s.postgres.name if st.stack_kind == "postgres" else None
            created, ca = self.stack.postgres_up(db, sec, branch_of=branch_of, app=True)
            if created:
                self._pg_init(f"{rd}/repo", sec, f"pg-init-{st.id}")
                st.database = f"`{db}`, a new Postgres with local/postgres-init"
            else:
                st.database = f"`{db}`, a branch of `{s.postgres.name}`: a fork of its data"
            env = self.stack.postgres_env(sec)
            files[CA_FILE] = ca.encode()
        st.sandbox_url = self.stack.app_up(sandbox, kind, env, pack(files))
        self.show(f"sandbox: {st.sandbox_url}")

    def _feature_check(self, st: RunState) -> None:
        """The AI's script on the sandbox, with nothing but PATH and HOME: no more than the AI itself could do."""
        try:
            st.feature_log = steps.feature_check(self.settings, st.id, st.sandbox_url)
        except RuntimeError as err:
            raise RuntimeError(f"the feature check fails on the sandbox: no pull request\n{err}") from None

    def _pr(self, st: RunState) -> None:
        s, pc, rd, n = self.settings, self.computer(), self.run_dir(st.id), self.names(st.id)
        v = pr.Validation(
            stack=s.app.computer, base_branch=s.repo.base_branch, sandbox=n.sandbox,
            checkpoint=n.checkpoint, kind=st.new_kind, database=st.database, url=st.sandbox_url, ai=s.ai,
            analysis=st.analysis,
            feature_check=(steps.run_dir(self.settings, st.id) / CHECK).read_text() if st.kind == "task" else "",
            feature_log=st.feature_log,
        )  # fmt: skip
        text = f"{st.title}\n\n{pr.body(st.text, v, st.id)}"
        (st.path / "pr.md").write_text(text)
        pc.upload(text.encode(), f"{rd}/pr.md")
        out = self._step(f"pr-{st.id}", "pr", st.id, env=self._token())
        st.pr_url = out.strip().splitlines()[-1]
        self.show(f"pull request: {st.pr_url}")

    # ------------------------------------------------------------------ report, status, stop

    def write_report(self, st: RunState) -> Path:
        result = None
        if (f := st.path / RESULT).is_file():
            result = json.loads(f.read_text())
        try:
            left = self.cleanup.left(st.id)
        except NeviaError:
            left = ["(could not list them)"]
        r = pr.Report(
            run=st.id, kind=st.kind, repo=self.settings.repo.github, base_branch=self.settings.repo.base_branch,
            started=st.started, outcome=st.outcome or "going", ai=self.settings.ai.name,
            steps=list(st.durations.items()), pr_url=st.pr_url, sandbox_url=st.sandbox_url, result=result, left=left,
        )  # fmt: skip
        path = st.path / REPORT
        path.write_text(pr.report(r))
        cost = cost_suffix(result, self.settings.ai.name)
        console.log(f"report: {path.relative_to(BUILD_DIR.parent)} ({st.outcome or 'going'}{cost})")
        return path

    def ai_running(self, run: str) -> bool:
        return self.computer().job_running(f"ai-{run}")

    def stop(self) -> None:
        """Stop the AI and the jobs of the run going on the agent computer; what it made stays (`clean`)."""
        pc = self.computer()
        latest = RunState.latest()
        # The orchestrator first, so that it starts no other step; then the jobs it may have going.
        for job in [RUN_JOB, BASE_PG_INIT_JOB, *(pc.jobs(f"*-{latest.id}") if latest else [])]:
            pc.stop_job(job)
        pc.sh(f"sudo -n pkill -u {AI_USER} || true")  # the AI's own processes: another user's, out of a job's reach
        if st := self.unfinished():
            st.outcome = "stopped"
            st.save()
        console.log(f"{self.name}: stopped")


# ---------------------------------------------------------------------- the agent computer's commands
# Your machine's commands (acc.local) run these there, through `agent acc remote`.

remote = remote_app()
LATEST = 15  # the AI's lines `status` shows


def _here() -> Agent:
    return Agent.of(Settings.load(), show=lambda line: out.print(line, markup=False, highlight=False))


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
        if "ai" not in st.done and (acts := events(ag.activity(st.id))):
            out.rule("the AI's latest actions" + (" (running)" if ag.ai_running(st.id) else ""))
            for a in acts[-LATEST:]:
                out.print(f"{a.time} {a.text}", markup=False, highlight=False, style="red" if a.failed else None)
    out.print(f"sandboxes: {', '.join(ag.cleanup.doomed().computers) or 'none'}")


@remote.command("stop")
def remote_stop() -> None:
    _here().stop()


@remote.command("clean")
def remote_clean(run: Annotated[str | None, typer.Option()] = None, yes: YesOpt = False) -> None:
    """Without --yes, what it would delete, as JSON."""
    ag = _here()
    if (st := ag.unfinished()) and ag.ai_running(st.id) and run in {None, st.id}:
        raise typer.BadParameter(f"run {st.id} is going: agent acc stop first")
    listed_or_deleted(ag.cleanup.doomed(run), yes=yes, delete=ag.cleanup.delete)


@remote.command("destroy")
def remote_destroy(yes: YesOpt = False) -> None:
    """Without --yes, what it would delete, as JSON."""
    ag = _here()
    listed_or_deleted(ag.cleanup.doomed_all(), yes=yes, delete=ag.cleanup.delete)


@remote.command("step")
def remote_step(name: str, arg: str) -> None:
    """A step that needs a secret, which this job's env holds: clone, analyse, ai, pr (`arg`: the run), or
    pg-init (`arg`: the clone, under the home)."""
    s = Settings.load()
    match name:
        case "clone":
            steps.clone(s, arg)
        case "analyse":
            steps.analyse(s, arg)
        case "ai":
            steps.ai_session(s, arg)
        case "pr":
            print(steps.open_pr(s, arg))  # noqa: T201 - read by the orchestrator
        case "pg-init":
            steps.pg_init(steps.HOME / arg)
        case _:
            raise typer.BadParameter(f"no step {name!r}")
