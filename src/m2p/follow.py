"""What `agent m2p run` and `status` print of the agent (your machine): its status, and, as it goes, a timeline of
its sessions, branches, tries and actions."""

import contextlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from core.activity import Activity
from core.computer import Computer, JobFailed
from core.shell import out
from m2p.run import AGENT_JOB, Status


class Followed(Protocol):
    """The agent as `follow` reads it (`m2p.local.Local`)."""

    def status(self) -> Status: ...

    def computer(self, poll: float = ...) -> Computer: ...


def print_status(st: Status) -> None:
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


def _clock(iso: str) -> str:
    """A session line's time (ISO 8601, UTC) as local `HH:MM:SS`, as the events' (in your time zone already) are."""
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M:%S")


def _event(when: str, text: str, style: str | None = None) -> None:
    out.print(f"[{when}] {text}", style=style, markup=False, highlight=False)


def print_activity(events: list[Activity], *, details: bool) -> None:
    for a in events:
        text = a.text if details else _clip(a.text.replace("says:", "agent:", 1))
        _event(a.time, text, "red" if a.failed or a.text.startswith("error:") else None)


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


def follow(lp: Followed, interval: float, *, details: bool = False) -> None:
    """Print what happens as it happens, until the agent's job ends (`Computer.wait_job`, which waits out a
    computer that doesn't answer for a while): sessions starting and ending, branches made and deleted, each
    try's result, and what the agent says and what fails (every action with `details`)."""
    sessions, acts, results = set[str](), set[str](), dict[int, str]()
    branches: set[str] | None = None
    last: list[Status] = []

    def show_status() -> None:
        nonlocal branches
        st = lp.status()
        now = datetime.now().strftime("%H:%M:%S")
        for line in st.sessions.splitlines():
            if line not in sessions:
                sessions.add(line)
                when, _, what = line.partition(" ")
                _event(_clock(when), what)
        # Without --details, what it says, its failures and errors.
        new = [a for a in st.activity if a.id not in acts and (details or a.brief)]
        print_activity(new if acts else new[-20:], details=details)  # at the start, the latest few only
        acts.update(a.id for a in st.activity)
        _branch_events(now, branches, set(st.branches))
        branches = set(st.branches)
        _try_events(now, results, _tries(st.attempts))
        last[:] = [st]

    show_status()
    with contextlib.suppress(JobFailed):  # stopped, or out of time: the status says how it ended
        lp.computer(interval).wait_job(AGENT_JOB, on_poll=show_status)
    st = last[0]
    ended = f"agent {'done' if st.done else 'stopped'}: {_summary(_tries(st.attempts))}"
    _event(datetime.now().strftime("%H:%M:%S"), ended)
    if details:
        out.rule()
        print_status(st)
