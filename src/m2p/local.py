"""`m2p`: an agent on Nevia works out the hello MongoDB to PostgreSQL migration with mongo2pg."""

import json
from pathlib import Path
from typing import Annotated

import typer

from core.config import RUN_ID, new_run_id, runs_dir
from core.local import frame
from core.local.collect import Collect
from core.local.collect import collect as collect_results
from core.nevia import Nevia, NeviaError
from core.results import REPORT, RESULT, usd
from core.shell import console, out, show
from m2p.computer import JOBS_DIR, Computer
from m2p.config import Settings
from m2p.follow import follow, print_activity, print_status
from m2p.results import FINAL_SQL, final_sql
from m2p.run import AGENT_JOB, Status, status_of

app = typer.Typer(no_args_is_help=True, help=__doc__, pretty_exceptions_show_locals=False)


# ---------------------------------------------------------------------- what your machine makes and brings back

RUNS_DIR = runs_dir("m2p")  # where `collect` puts each run's results, at the end of `run`
# Left behind by `collect`: our tools and binary, the checkout, which is there to read, and each load's
# export (MongoDB's data as CSV, which only makes the archive big). Other files over core.local.collect's limit
# (dumps) are listed only.
COLLECT_SKIP = ("bin", "tools", "mongo2pg", "tries/*/data")


def run_id(pc: Computer, loop_dir: str) -> str:
    """The run's id, `yymmddHHMMSS` (UTC) of its start, as acc's are: the agent's `run` writes it in LOOP/RUN."""
    rid = pc.sh(f"cat ~/{loop_dir}/RUN 2>/dev/null || true").strip()
    return rid if RUN_ID.fullmatch(rid) else new_run_id()


def collect(s: Settings, nevia: Nevia, dest: Path | None = None) -> Path:
    """The run's results here, in `build/m2p-runs/<run>/` (`core.local.collect`, as acc's): the agent's files in
    LOOP (notes, scripts, its mongo2pg projects in tries/, `logs/ai-N.*`, one per AI session, and the run's
    result.json and report.md, as acc's) and the agent job's log; then `final.sql`, all the SQL the target was
    loaded with."""
    pc = Computer(s.agent.computer, nevia, settings=s)
    dest = dest or RUNS_DIR / run_id(pc, s.agent.dir)
    c = Collect([s.agent.dir], COLLECT_SKIP, {"agent-job.log": f"{JOBS_DIR}/{AGENT_JOB}.log"})
    try:
        collect_results(pc, c, dest)
    except NeviaError as err:
        raise NeviaError(
            f"{err}; the results are still there, and `agent m2p run` collects them again (once DONE.md exists, "
            "it starts no new session)"
        ) from None
    if final := final_sql(dest / "tries" / "final", s.postgres.name):
        (dest / FINAL_SQL).write_text(final)
        console.log(f"the SQL {s.postgres.name} was loaded with: {dest / FINAL_SQL}")
    if (f := dest / RESULT).is_file() and "total_cost_usd" in (res := json.loads(f.read_text())):
        console.log(f"{s.ai.name}: {usd(res['total_cost_usd'])} over {res.get('sessions', '?')} sessions "
                    f"(its estimate, at list prices): {dest / REPORT}")  # fmt: skip
    return dest


class Local(frame.Local[Settings]):
    """The agent as your machine sees it: everything but `collect` (which reads its computer's files) runs on
    the agent computer, through `agent m2p remote` (m2p.agent)."""

    def computer(self, poll: float = 20.0) -> Computer:
        return Computer(self.name, self.nevia, poll, settings=self.settings)

    def _remote(self, *args: str) -> str:
        return self.bootstrap().remote(*args)

    def running(self) -> bool:
        pc = self.computer()
        pc.awake()
        return pc.job_running(AGENT_JOB)

    def run(self) -> None:
        show(self._remote("run"))

    def status(self) -> Status:
        return status_of(self.bootstrap().remote_json("status"))

    def stop(self) -> None:
        self._remote("stop")

    def clean(self, *, yes: bool) -> None:
        """What `remote clean` lists, listed and asked about (unless `yes`), then deleted."""
        s = self.settings
        self.bootstrap().listed_then_deleted(
            "clean", yes=yes,
            question=f"Delete these? ~/{s.agent.dir} on {self.name} keeps only what setup put there (`run` collected "
            f"it into build/m2p-runs), and schema {s.mongo.database} in {s.postgres.name} goes too",
        )  # fmt: skip

    def collect(self) -> Path:
        return collect(self.settings, self.nevia)


def _agent() -> Local:
    return Local.of(Settings.load())


frame.setup_command(
    app, _agent,
    "Make the agent computer, its key and the gateway key's secret, send it this code; then, from there, Mongo\n"
    "(a fresh branch of the seed computer, seeded again only when [m2p.seed] changed), Postgres, the agent's\n"
    "tools and its link to Mongo. Safe to repeat.",
)  # fmt: skip


Interval = Annotated[float, typer.Option(help="Seconds between checks.")]
Details = Annotated[bool, typer.Option(help="Every action (commands, edits, messages, failures) and the full status.")]


@app.command("run")
def agent_run(interval: Interval = 10, details: Details = False) -> None:
    """Start the agent, follow it until it stops, then collect its results to build/m2p-runs.

    It prints what the agent sets out to do and its errors as they happen (every action with --details),
    and a one-line status whenever the tries, sessions or branches change.

    With the agent already running (after a Ctrl-C here, which leaves it running), follow that run instead.
    At its end the target and the try branches get new passwords (the AI's copies stop working); after one that
    passed, the target is protected and copied into <target>-loaded. Once it is over and collected, the agent
    computer sleeps.
    """
    lp = _agent()
    if lp.running():
        out.print(f"the agent is already running on {lp.name}: following it")
    else:
        lp.run()
    with lp.asleep_after(AGENT_JOB):
        follow(lp, interval, details=details)
        lp.collect()


@app.command("status")
def agent_status(
    last: Annotated[int, typer.Option(help="How many of the agent's latest actions to show.")] = 15,
) -> None:
    """The agent's state, branches, memory, sessions, tries so far, Nevia's operations still running, and its
    latest actions."""
    lp = _agent()
    st = lp.status()
    print_status(st)
    lp.show_operations()
    if st.activity and last:
        out.rule("latest actions")
        print_activity(st.activity[-last:], details=True)


@app.command("stop")
def agent_stop() -> None:
    """Stop the agent; its branches and files stay. A `run` following it then collects its results."""
    _agent().stop()


@app.command("clean")
def agent_clean(yes: frame.Yes = False) -> None:
    """Remove the last run's work: the agent's files, its try branches and the target's schema, listed first."""
    _agent().clean(yes=yes)


frame.destroy_command(
    app, _agent,
    "Delete everything `setup` made on Nevia: the agent deletes Mongo and its seed computer, Postgres, its copy\n"
    "and its branches (clearing their protection), then your machine the agent computer, its key and the secrets.",
)  # fmt: skip
