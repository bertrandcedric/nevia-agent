"""`acc`: an AI agent on Nevia changes an app and opens a pull request, once a sandbox has passed."""

import shlex
from pathlib import Path
from typing import Annotated

import typer

from acc import config
from acc.computer import JOBS_DIR, Computer
from acc.run import RUN_JOB, RUNS_DIR, RunState, run_dir_of
from acc.run import FromOpt as From
from acc.run import ModeArg as Kind
from acc.run import YesOpt as Yes
from core.computer import AGENT_BUILD_DIR, JobFailed
from core.local import frame, runtime
from core.local.bootstrap import Key, Wanted
from core.local.collect import Collect
from core.local.collect import collect as collect_results
from core.shell import out, show

app = typer.Typer(no_args_is_help=True, help=__doc__, pretty_exceptions_show_locals=False)
TASK_FILE = f"{runtime.HOME}/task.md"  # your request for `run task`, sent to the agent computer


# ---------------------------------------------------------------------- what your machine makes and brings back


def latest_run(pc: Computer) -> str:
    """The id of the latest run on the agent computer, or nothing."""
    runs = f"~/{AGENT_BUILD_DIR}/{RUNS_DIR.name}"
    return pc.sh(f"ls -1 {runs} 2>/dev/null | sort | tail -n 1").strip()


def collect(s: config.Settings, pc: Computer, run: str) -> Path:
    """The run's results here, in `build/acc-runs/<run>/` (`core.local.collect`, as m2p's): its directory on the
    agent computer (the AI's logs and result, the analysis, the feature check), its state, report and pull
    request text, the orchestrator's log, and `change.patch`, the change against the base branch; the clone
    itself stays there."""
    rd, base = run_dir_of(s.agent.dir, run), s.repo.base_branch
    c = Collect(
        [rd, f"{AGENT_BUILD_DIR}/{RUNS_DIR.name}/{run}"],
        ("repo",),
        {"agent-job.log": f"{JOBS_DIR}/{RUN_JOB}.log"},
        # The change against the base branch, whether the run committed it or not.
        f"cd ~/{rd}/repo && git add -A && git diff --cached --no-color origin/{shlex.quote(base)} "
        '> "$STAGE/change.patch"',
    )
    return collect_results(pc, c, RUNS_DIR / run)


Task = Annotated[str, typer.Argument(help="What to build, for `task`.")]
TaskFile = Annotated[Path | None, typer.Option("--file", "-f", help="What to build, in a file.", exists=True)]


class Local(frame.Local[config.Settings]):
    """acc as your machine sees it: its settings, the agent computer, and what makes it (`bootstrap`): the
    agent computer, its key and its AI's (a viewer's), and the secrets of the GitHub token (required the first
    time) and of the gateway key. The rest runs there, through `agent acc remote` (acc.agent)."""

    def computer(self) -> Computer:
        return Computer(self.name, self.nevia)

    def wanted(self) -> list[Wanted]:
        return [Wanted(self.settings.repo.token_secret, always=False)]

    def other_keys(self) -> list[Key]:
        a = self.settings.agent
        return [Key(a.ai_principal, "viewer", "acc agent's AI", a.ai_secret, a.ai_key_path)]


def _agent() -> Local:
    return Local.of(config.Settings.load())


# ---------------------------------------------------------------------- your machine


frame.setup_command(
    app, _agent,
    "Make the agent computer, its key and the secrets from .env, send it this code; then, from there, its\n"
    "tools, the AI for its own user, and mongo2pg. The app's stack comes with each run. Safe to repeat.",
)  # fmt: skip


def _follow(ag: Local) -> None:
    """The orchestrator's log as it goes, until it ends; then the run's files, into build/acc-runs/<run>/."""
    failed = None
    with ag.asleep_after(RUN_JOB):  # `resume` and every other command wake it
        try:
            ag.computer().follow_job(RUN_JOB, show)
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
    state = collect(ag.settings, ag.computer(), run) / "state.json"
    return RunState.model_validate_json(state.read_text()) if state.is_file() else None


@app.command()
def run(kind: Kind, task: Task = "", file: TaskFile = None, step: From = None) -> None:
    """Run the agent on the repository, follow it, and bring its report back once it is over.

    `migrate` moves the app from MongoDB to Postgres; `task "<feature>"` builds a feature, checked on a fork of
    the data; `resume` takes the last run up from the step that did not finish (`--from STEP`: from that one,
    e.g. `--from sandbox`, without the AI again), or follows it if it is going. The run goes on on the agent
    computer: Ctrl-C only stops following it. Once it is over and collected, the agent computer sleeps.
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
    """The last run's steps and outcome, the AI's latest actions, the sandboxes on Nevia, and its operations
    still running."""
    ag = _agent()
    show(ag.bootstrap().remote("status"))
    ag.show_operations()


@app.command()
def stop() -> None:
    """Stop the run going on the agent computer, the AI included; what it made stays (`agent acc clean`)."""
    show(_agent().bootstrap().remote("stop"))


@app.command()
def clean(
    run: Annotated[str | None, typer.Option(help="Only this run's (default: every run's).")] = None,
    yes: Yes = False,
) -> None:
    """Delete what runs left: sandboxes (the app's and its database's), checkpoints, databases, secrets and the
    runs' files on the agent computer. The stack, the agent and the collected reports in build/ stay."""
    only = ["--run", run] if run else []
    _agent().bootstrap().listed_then_deleted(
        "clean",
        *only,
        yes=yes,
        nothing="nothing of a run: no sandbox, checkpoint, database or secret",
        files_alone=False,
    )


frame.destroy_command(
    app, _agent,
    "Delete everything acc made on Nevia: the agent deletes the sandboxes, the app and its databases (all\n"
    "data) and the secrets it made, then your machine the agent computer, its key and the secrets from .env.\n"
    "Collected reports in build/ stay.",
)  # fmt: skip
