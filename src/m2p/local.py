"""`m2p`: an agent on Nevia works out the hello MongoDB to PostgreSQL migration with mongo2pg."""

import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from core.config import BUILD_DIR
from core.local.bootstrap import Bootstrap, Wanted
from core.local.collect import Collect
from core.local.collect import collect as collect_results
from core.nevia import Nevia, NeviaError
from core.shell import console, out, show
from m2p import agent, config
from m2p.agent import AGENT_JOB, Activity, Status, status_of
from m2p.computer import JOBS_DIR, Computer

app = typer.Typer(no_args_is_help=True, help=__doc__, pretty_exceptions_show_locals=False)
app.add_typer(agent.remote, name="remote")  # what the commands do on the agent computer


def _settings() -> config.Settings:
    return config.load()


# ---------------------------------------------------------------------- agent


# ---------------------------------------------------------------------- what your machine makes and brings back

RUNS_DIR = BUILD_DIR / "m2p-runs"  # where `collect` puts each run's results, at the end of `run`
# Left behind by `collect`: our tools and binary, the checkout, which is there to read, and each load's
# export (MongoDB's data as CSV, which only makes the archive big). Other files over core.local.collect's limit
# (dumps) are listed only.
COLLECT_SKIP = ("bin", "tools", "mongo2pg", "tries/*/data")
RUN_ID = re.compile(r"\d{12}")
RUN_ID = re.compile(r"\d{12}")


FINAL_SQL = "final.sql"  # next to DONE.md in the collected results


APPLIED = re.compile(r"Created PostgreSQL objects from (\S+\.sql)")  # mongo2pg import's line for each DDL file


def applied_ddl(project: Path) -> list[Path]:
    """The DDL files mongo2pg's import ran, in order, from the project's logs; all of `schema/` if no log
    says. The paths in the logs are the agent computer's: only their part under the project counts."""
    applied = []
    for log in sorted(project.rglob("*.log")):
        for m in APPLIED.finditer(log.read_text(errors="replace")):
            rel = m[1].split(f"/{project.name}/", 1)[-1]
            if (f := project / rel).is_file() and f not in applied:
                applied.append(f)
    return applied or sorted((project / "schema").rglob("*.sql"))


def final_sql(project: Path, target: str) -> str:
    """The SQL the final load ran, in order: the DDL mongo2pg's import applied, then the data fix the agent
    ran after the import, if any. Empty when the project has no DDL."""
    ddl = applied_ddl(project) if project.is_dir() else []
    if not ddl:
        return ""
    parts = [f"-- The SQL {target} was loaded with from tries/{project.name}/, in the order it ran.\n"]
    parts += [f"\n-- {p.relative_to(project)}, applied by mongo2pg import\n{p.read_text()}" for p in ddl]
    if (fix := project / "normalize.sql").is_file():
        parts.append(f"\n-- normalize.sql, run after the import\n{fix.read_text()}")
    return "".join(parts)


def bootstrap(s: config.Settings, nevia: Nevia) -> Bootstrap:
    """What your machine makes on Nevia for m2p: the agent computer, its key, the gateway key's secret."""
    pc = Computer(s.agent.computer, nevia, settings=s)
    wanted = [Wanted(s.ai.gateway_secret, s.ai.gateway_secret)] if s.ai.gateway_url else []
    return Bootstrap("m2p", pc, s.runtime, s.secret, s.agent.secret, s.agent.key_path, wanted)


def run_id(pc: Computer, loop_dir: str) -> str:
    """The run's id, `yymmddHHMMSS` (UTC) of its start, as acc's are: the agent's `run` writes it in LOOP/RUN."""
    rid = pc.sh(f"cat ~/{loop_dir}/RUN 2>/dev/null || true").strip()
    return rid if RUN_ID.fullmatch(rid) else datetime.now(UTC).strftime("%y%m%d%H%M%S")


def collect(s: config.Settings, nevia: Nevia, dest: Path | None = None) -> Path:
    """The run's results here, in `build/m2p-runs/<run>/` (`core.local.collect`, as acc's): the agent's files in
    LOOP (notes, scripts, its mongo2pg projects in tries/, and `logs/session-N.log`, one per AI session)
    and the agent job's log; then `final.sql`, all the SQL the target was loaded with."""
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
    return dest


@dataclass
class Local:
    """The agent as your machine sees it: everything but `collect` (which reads its computer's files) runs on
    the agent computer, through `agent m2p remote` (m2p.agent)."""

    settings: config.Settings
    nevia: Nevia

    @property
    def name(self) -> str:
        return self.settings.agent.computer

    def bootstrap(self) -> Bootstrap:
        return bootstrap(self.settings, self.nevia)

    def _remote(self, *args: str) -> str:
        return self.bootstrap().remote(*args)

    def running(self) -> bool:
        return self._remote("running").strip() == "yes"

    def run(self) -> None:
        show(self._remote("run"))

    def status(self) -> Status:
        return status_of(self._remote("status").strip().splitlines()[-1])

    def stop(self) -> None:
        self._remote("stop")

    def clean(self) -> None:
        show(self._remote("clean"))

    def collect(self) -> Path:
        return collect(self.settings, self.nevia)


def _agent() -> Local:
    s = _settings()
    return Local(s, Nevia(s.nevia.workspace))


def _print_status(st: Status) -> None:
    state = "[green]done" if st.done else "running" if st.running else "[yellow]not running"
    out.print(f"agent: {state}   branches: {', '.join(st.branches) or 'none'}")
    out.print(" ".join(f"{k}={v}" for k, v in st.memory.items()))
    if st.sessions:
        out.print(st.sessions)
    if st.attempts:
        out.print(st.attempts)


@dataclass(frozen=True)
class Try:
    n: int
    result: str  # PASSED or FAILED
    happened: str
    change: str


def _tries(attempts: str) -> list[Try]:
    """ATTEMPTS.md's tries, from their `## Try N: PASSED or FAILED` headings and `- What happened:` and
    `- Change:` lines."""
    found = []
    for block in re.split(r"^(?=## Try )", attempts, flags=re.MULTILINE):
        if m := re.match(r"## Try (\d+): (\w+)", block):
            fields = dict(re.findall(r"^- (What happened|Change): (.*)$", block, flags=re.MULTILINE))
            found.append(Try(int(m[1]), m[2].upper(), fields.get("What happened", ""), fields.get("Change", "")))
    return found


def _summary(tries: list[Try]) -> str:
    if not tries:
        return "no tries"
    return f"{len(tries)} {'try' if len(tries) == 1 else 'tries'}, the last {tries[-1].result}"


def _clip(text: str, n: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _local(utc: str) -> str:
    """A time of today in UTC, `HH:MM:SS` or ISO, as local `HH:MM:SS`, as m2p's own log lines have it."""
    if "T" in utc:
        when = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    else:
        when = datetime.combine(datetime.now(UTC).date(), datetime.strptime(utc, "%H:%M:%S").time(), tzinfo=UTC)
    return when.astimezone().strftime("%H:%M:%S")


def _event(when: str, text: str, style: str | None = None) -> None:
    out.print(f"[{when}] {text}", style=style, markup=False, highlight=False)


def _brief(a: Activity) -> bool:
    """What `run` shows of the agent's actions without --details: what it says, its failures and errors."""
    return a.brief


def _print_activity(events: list[Activity], *, details: bool) -> None:
    for a in events:
        text = a.text if details else _clip(a.text.replace("says:", "agent:", 1))
        _event(_local(a.time), text, "red" if a.failed or a.text.startswith("error:") else None)


@app.command("setup")
def agent_setup() -> None:
    """Make the agent computer, its key and the gateway key's secret, send it this code; then, from there, Mongo,
    Postgres, the agent's tools and its link to Mongo. Safe to repeat."""
    _agent().bootstrap().setup(dict(os.environ))


Interval = Annotated[float, typer.Option(help="Seconds between checks.")]
Details = Annotated[bool, typer.Option(help="Every action (commands, edits, messages, failures) and the full status.")]


def _branch_events(now: str, before: set[str] | None, after: set[str]) -> None:
    if before is None:
        if after:
            _event(now, f"branches: {', '.join(sorted(after))}")
        return
    for b in sorted(after - before):
        _event(now, f"branch {b} created")
    for b in sorted(before - after):
        _event(now, f"branch {b} deleted")


def _try_events(now: str, results: dict[int, str], tries: list[Try]) -> None:
    """Each try whose result is new, and for a failed one what the agent changes next; `results` keeps track."""
    for t in tries:
        if results.get(t.n) != t.result:
            results[t.n] = t.result
            _event(now, f"try {t.n} {t.result}: {_clip(t.happened)}", "green" if t.result == "PASSED" else "red")
            if t.result != "PASSED" and t.change:
                _event(now, f"  next: {_clip(t.change)}")


def _follow(lp: Local, interval: float, *, details: bool = False) -> None:
    """Print what happens as it happens, until the agent stops: sessions starting and ending, branches made
    and deleted, each try's result, and what the agent says and what fails (every action with `details`)."""
    sessions, acts, results = set[str](), set[str](), dict[int, str]()
    branches: set[str] | None = None
    while True:
        try:
            st = lp.status()
        except (RuntimeError, NeviaError) as err:
            out.print(f"[red]unreachable: {err}")
            time.sleep(interval)
            continue
        now = datetime.now().strftime("%H:%M:%S")
        for line in st.sessions.splitlines():
            if line not in sessions:
                sessions.add(line)
                when, _, what = line.partition(" ")
                _event(_local(when), what)
        new = [a for a in st.activity if a.id not in acts and (details or _brief(a))]
        _print_activity(new if acts else new[-20:], details=details)  # at the start, the latest few only
        acts.update(a.id for a in st.activity)
        _branch_events(now, branches, set(st.branches))
        branches = set(st.branches)
        _try_events(now, results, _tries(st.attempts))
        if not st.running:
            _event(now, f"agent {'done' if st.done else 'stopped'}: {_summary(_tries(st.attempts))}")
            if details:
                out.rule()
                _print_status(st)
            return
        time.sleep(interval)


@app.command("run")
def agent_run(interval: Interval = 10, details: Details = False) -> None:
    """Start the agent, follow it until it stops, then collect its results to build/m2p-runs.

    It prints what the agent sets out to do and its errors as they happen (every action with --details),
    and a one-line status whenever the tries, sessions or branches change.

    With the agent already running (after a Ctrl-C here, which leaves it running), follow that run instead.
    """
    lp = _agent()
    if lp.running():
        out.print(f"the agent is already running on {lp.name}: following it")
    else:
        lp.run()
    _follow(lp, interval, details=details)
    lp.collect()


@app.command("status")
def agent_status(
    last: Annotated[int, typer.Option(help="How many of the agent's latest actions to show.")] = 15,
) -> None:
    """The agent's state, branches, memory, sessions, tries so far and latest actions."""
    st = _agent().status()
    _print_status(st)
    if st.activity and last:
        out.rule("latest actions")
        _print_activity(st.activity[-last:], details=True)


@app.command("stop")
def agent_stop() -> None:
    """Stop the agent; its branches and files stay. A `run` following it then collects its results."""
    _agent().stop()


@app.command("clean")
def agent_clean(yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask first.")] = False) -> None:
    """Remove the last run's work: the agent's files, the m2p-try branches and the target's schema."""
    ag = _agent()
    s = ag.settings
    if not yes:
        typer.confirm(
            f"Delete the agent's work in ~/{s.agent.dir} on {ag.name} (`run` collected it into build/m2p-runs), "
            f"the m2p-try branches, and schema {s.mongo.database} in {s.postgres.name}?",
            abort=True,
        )
    ag.clean()


@app.command("destroy")
def agent_destroy(yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask first.")] = False) -> None:
    """Delete everything `setup` made on Nevia: the agent deletes Mongo, Postgres and its branches, then your
    machine the agent computer, its key and the secrets."""
    _agent().bootstrap().destroy(yes=yes)
