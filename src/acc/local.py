"""`acc`: a Claude Code agent on Nevia changes an app and opens a pull request, once a sandbox has passed."""

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from nevia_kit import Nevia
from rich.console import Console

from acc import agent, config
from acc.agent import RUN_JOB, RUNS_DIR, RunState
from acc.agent import FromOpt as From
from acc.agent import ModeArg as Kind
from acc.agent import YesOpt as Yes
from acc.computer import JOBS_DIR, Computer
from core.computer import JobFailed
from core.doomed import Doomed
from core.local import runtime
from core.local.bootstrap import Bootstrap, Wanted
from core.local.collect import Collect
from core.local.collect import collect as collect_results

app = typer.Typer(no_args_is_help=True, help=__doc__, pretty_exceptions_show_locals=False)
app.add_typer(agent.remote, name="remote")  # what the commands do on the agent computer
out = Console(soft_wrap=True)
LATEST = 15  # Claude's lines `status` shows
TASK_FILE = f"{runtime.HOME}/task.md"  # your request for `run task`, sent to the agent computer


# ---------------------------------------------------------------------- what your machine makes and brings back


def bootstrap(s: config.Settings, nevia: Nevia) -> Bootstrap:
    """What your machine makes on Nevia for acc: the agent computer, its key, and the secrets of the GitHub
    token (required the first time) and of the gateway key."""
    c = s.claude
    wanted = [Wanted(s.repo.token_secret, s.repo.token_secret, always=False)]
    if c.gateway_url:
        wanted.append(Wanted(c.gateway_secret, c.gateway_secret))
    pc = Computer(s.agent.computer, nevia)
    return Bootstrap("acc", pc, s.runtime, s.secret, s.agent.secret, s.agent.key_path, wanted)


def latest_run(pc: Computer) -> str:
    """The id of the latest run on the agent computer, or nothing."""
    runs = f"~/{runtime.HOME}/build/{RUNS_DIR.name}"
    return pc.sh(f"ls -1 {runs} 2>/dev/null | sort | tail -n 1").strip()


def collect(s: config.Settings, pc: Computer, run: str) -> Path:
    """The run's results here, in `build/acc-runs/<run>/` (`core.local.collect`, as m2p's): its directory on the
    agent computer (Claude's logs and result, the analysis, the feature check), its state, report and pull
    request text, the orchestrator's log, and `change.patch`, the change against the base branch; the clone
    itself stays there."""
    rd, base = f"{s.agent.dir}/runs/{run}", s.repo.base_branch
    c = Collect(
        [rd, f"{runtime.HOME}/build/{RUNS_DIR.name}/{run}"],
        ("repo",),
        {"agent-job.log": f"{JOBS_DIR}/{RUN_JOB}.log"},
        # The change against the base branch, whether the run committed it or not.
        f"cd ~/{rd}/repo && git add -A && git diff --cached --no-color origin/{shlex.quote(base)} "
        '> "$STAGE/change.patch"',
    )
    return collect_results(pc, c, RUNS_DIR / run)


Task = Annotated[str, typer.Argument(help="What to build, for `task`.")]
TaskFile = Annotated[Path | None, typer.Option("--file", "-f", help="What to build, in a file.", exists=True)]


@dataclass
class Local:
    """acc as your machine sees it: its settings, the agent computer, and what makes it (`bootstrap`). The rest
    runs there, through `agent acc remote` (acc.agent)."""

    settings: config.Settings
    nevia: Nevia

    @property
    def name(self) -> str:
        return self.settings.agent.computer

    def computer(self) -> Computer:
        return Computer(self.name, self.nevia)

    def bootstrap(self) -> Bootstrap:
        return bootstrap(self.settings, self.nevia)


def _agent() -> Local:
    s = config.load()
    return Local(s, Nevia(s.nevia.workspace))


def _line(text: str) -> None:
    out.print(text, markup=False, highlight=False)


def _print(text: str) -> None:
    if text.strip():
        out.print(text.rstrip(), markup=False, highlight=False)


# ---------------------------------------------------------------------- your machine


@app.command()
def setup() -> None:
    """Make the agent computer, its key and the secrets from .env, send it this code; then, from there, its
    tools, Claude Code for its own user, and mongo2pg. The app's stack comes with each run. Safe to repeat."""
    boot = _agent().bootstrap()
    boot.up(dict(os.environ))
    boot.remote_job("acc-setup", "setup")
    boot.computer.follow_job("acc-setup", _line)


def _follow(ag: Local) -> None:
    """The orchestrator's log as it goes, until it ends; then the run's files, into build/acc-runs/<run>/."""
    failed = None
    try:
        ag.computer().follow_job(RUN_JOB, _line)
    except JobFailed as err:
        failed = err
    st = _collect(ag)
    if failed:
        raise RuntimeError(
            f"run {st.id if st else '?'} failed: `agent acc run resume` takes it up, `agent acc clean` deletes it"
        )
    if st and st.pr_url:
        out.print(f"[green]pull request:[/] {st.pr_url}")


def _collect(ag: Local) -> RunState | None:
    """The latest run's results, from the agent computer (`collect`)."""
    run = latest_run(ag.computer())
    if not run:
        return None
    dest = collect(ag.settings, ag.computer(), run)
    return RunState.load(run) if (dest / "state.json").is_file() else None


@app.command()
def run(kind: Kind, task: Task = "", file: TaskFile = None, step: From = None) -> None:
    """Run the agent on the repository, follow it, and bring its report back once it is over.

    `migrate` moves the app from MongoDB to Postgres; `task "<feature>"` builds a feature, checked on a fork of
    the data; `resume` takes the last run up from the step that did not finish (`--from STEP`: from that one,
    e.g. `--from sandbox`, without Claude again), or follows it if it is going. The run goes on on the agent
    computer: Ctrl-C only stops following it.
    """
    if step and kind != "resume":
        raise typer.BadParameter("--from goes with resume: agent acc run resume --from STEP")
    ag = _agent()
    pc = ag.computer()
    pc.awake()
    if pc.job_running(RUN_JOB):
        if kind != "resume" or step:
            raise typer.BadParameter("a run is going: agent acc run resume follows it, agent acc stop stops it")
        out.print(f"a run is going on {ag.name}: following it")
    else:
        args: list[str] = [kind, *(["--from", step] if step else [])]
        if kind == "task":
            text = file.read_text() if file else task
            if not text.strip():
                raise typer.BadParameter("what to build? agent acc run task '<the feature>' (or --file)")
            ag.computer().upload(text.encode(), TASK_FILE)
            args += ["--file", f"~/{TASK_FILE}"]
        ag.bootstrap().remote_job(RUN_JOB, "run", *args)
    _follow(ag)


@app.command()
def status() -> None:
    """The last run's steps and outcome, Claude's latest actions, and the sandboxes on Nevia."""
    _print(_agent().bootstrap().remote("status"))


@app.command()
def stop() -> None:
    """Stop the run going on the agent computer, Claude included; what it made stays (`agent acc clean`)."""
    _print(_agent().bootstrap().remote("stop"))


@app.command()
def clean(
    run: Annotated[str | None, typer.Option(help="Only this run's (default: every run's).")] = None,
    yes: Yes = False,
) -> None:
    """Delete what runs left: sandboxes (the app's and its database's), checkpoints, databases, secrets and the
    runs' files on the agent computer. The stack, the agent and the collected reports in build/ stay."""
    boot = _agent().bootstrap()
    only = ["--run", run] if run else []
    d = Doomed.from_json(boot.remote("clean", *only))
    if not d.on_nevia:
        out.print("nothing of a run: no sandbox, checkpoint, database or secret")
        return
    for line in d.lines():
        out.print(line)
    if not yes:
        typer.confirm("Delete all of these?", abort=True)
    _print(boot.remote("clean", *only, "--yes"))


@app.command()
def destroy(yes: Yes = False) -> None:
    """Delete everything acc made on Nevia: the agent deletes the sandboxes, the app and its databases (all
    data) and the secrets it made, then your machine the agent computer, its key and the secrets from .env.
    Collected reports in build/ stay."""
    boot = _agent().bootstrap()
    mine = boot.doomed()
    theirs = Doomed.from_json(boot.remote("destroy")) if mine.computers else Doomed()
    if not (mine or theirs):
        out.print("nothing to delete")
        return
    for line in (theirs + mine).lines():
        out.print(line)
    if not yes:
        typer.confirm("Delete all of these? It can't be undone", abort=True)
    if theirs:
        _print(boot.remote("destroy", "--yes"))
    boot.teardown(mine)
