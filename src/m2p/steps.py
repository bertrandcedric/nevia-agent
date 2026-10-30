"""m2p's work on the agent computer that isn't the loop's own logic (`m2p.agent`): the computer's setup, and the
AI sessions themselves, as `agent m2p remote step loop` runs them (a job: the gateway key is in its env)."""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.agent import ai
from core.agent.host import HOME, lib, run
from core.shell import console
from m2p.config import Settings


def loop_dir(s: Settings) -> Path:
    return HOME / s.agent.dir


def setup(s: Settings) -> None:
    """git, jq, psql and the Python drivers; the AI agent (`core.agent.ai`); an upstream checkout of mongo2pg at
    the release, for reading only: the agent runs the release's binary in LOOP/bin, never a build."""
    d, m = loop_dir(s), s.mongo2pg
    for sub in ("bin", "tools", "tries", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (HOME / ai.BIN).mkdir(parents=True, exist_ok=True)  # the nevia CLI's too
    lib("apt_install git jq curl postgresql-client python3-pymongo python3-psycopg")
    version = ai.install(s.ai, s.agent.computer)
    checkout(d / "mongo2pg", m.repo, m.release)
    if not (d / "ATTEMPTS.md").exists():
        (d / "ATTEMPTS.md").write_text("# Attempts\n")
    console.log(f"agent ready: {s.ai.name} {version}, "
        f"mongo2pg at {run(['git', '-C', str(d / 'mongo2pg'), 'rev-parse', '--short', 'HEAD']).strip()}")  # fmt: skip


def checkout(src: Path, repo: str, ref: str) -> None:
    """mongo2pg's source at `ref`, on a branch `agent`; one made for another ref moves to this one, unless the
    agent has committed to it already."""

    def git(*args: str) -> str:
        return run(["git", "-C", str(src), *args])

    if not (src / ".git").is_dir():
        run(["git", "clone", "-q", repo, str(src)])
        git("switch", "-q", "-c", "agent", ref)
        git("config", "user.name", "m2p agent")
        git("config", "user.email", "agent@localhost")
    try:
        want = git("rev-parse", "-q", "--verify", f"{ref}^{{commit}}").strip()
    except RuntimeError:
        git("fetch", "-q", "--tags", "origin")
        want = git("rev-parse", f"{ref}^{{commit}}").strip()
    if subprocess.run(["git", "-C", str(src), "merge-base", "--is-ancestor", want, "HEAD"], check=False).returncode:
        if not git("log", "-1", "--committer=agent@localhost", "--format=%h").strip():
            git("switch", "-q", "-f", "-C", "agent", want)
        else:
            console.log(f"warning: {src} has the agent's commits on another base than {ref}; left as it is")


def loop(s: Settings, sessions: int) -> str:
    """The AI agent on the migration until DONE.md exists: up to `sessions` headless runs, each picking up from
    ATTEMPTS.md, with AGENTS.md as its prompt, allowed everything (the computer is its own). Each writes
    `logs/ai-N.log` (`core.agent.ai.log`, as acc's): its start and end lines (with Claude Code's cost), and its
    events as tools/render-<engine>.jq renders them, as they come; then its result in `logs/ai-N.json`, and the
    run's so far in result.json and report.md. The gateway key is in this step's env, for the AI only."""
    d = loop_dir(s)
    for f in (f"tools/{ai.render(s.ai)}", "tools/common.jq", "AGENTS.md"):
        if not (d / f).is_file():
            raise RuntimeError(f"{f} is missing: run agent m2p setup")
    cmd = ai.command(s.ai, (d / "AGENTS.md").read_text(), home=HOME)  # the guide is each session's prompt
    env = os.environ | {"PATH": f"{HOME}/.local/bin:{os.environ.get('PATH', '')}"}
    ended: list[tuple[int, int, dict[str, Any] | None]] = []  # session, exit code, result
    for n in range(1, sessions + 1):
        if (d / "DONE.md").exists():
            break
        log = ai.log(d, n)
        log.parent.mkdir(exist_ok=True)
        log.write_text(f"{_now()} session {n} started\n")
        rc = ai.session(cmd, cwd=d, env=env, render=d / "tools" / ai.render(s.ai), log=log)
        result = ai.result(s.ai, log.with_suffix(".jsonl").read_text().splitlines())
        if result is not None:
            log.with_suffix(".json").write_text(json.dumps(result) + "\n")
        cost = f", ${float(result['total_cost_usd']):.2f}" if result and "total_cost_usd" in result else ""
        with log.open("a") as out:
            out.write(f"{_now()} session {n} ended with {rc}{cost}\n")
        ended.append((n, rc, result))
        write_report(s, ended)
    outcome = "done" if (d / "DONE.md").exists() else f"no DONE.md after {sessions} sessions"
    total = ai.total([r for _, _, r in ended if r])
    return outcome + (f", {s.ai.name} ${float(total['total_cost_usd']):.2f}" if "total_cost_usd" in total else "")


def write_report(s: Settings, ended: list[tuple[int, int, dict[str, Any] | None]]) -> None:
    """The run's result.json (`core.agent.ai.total` of its sessions') and report.md, in LOOP, as acc's run has
    them: its outcome, each session's end, and the AI's turns, cost and tokens."""
    d = loop_dir(s)
    total = ai.total([r for _, _, r in ended if r])
    (d / ai.RESULT).write_text(json.dumps(total) + "\n")
    run = (d / "RUN").read_text().strip() if (d / "RUN").is_file() else "?"
    out = [f"# Run {run}", "", f"- migration of `{s.mongo.database}` to `{s.postgres.name}`: "
           + ("done" if (d / "DONE.md").exists() else "no DONE.md"), "", "## Sessions", "",
           "| Session | Exit | Turns | Duration | Cost |", "|---|---:|---:|---:|---:|"]  # fmt: skip
    for n, rc, result in ended:
        r = result or {}
        cost = ai.usd(r["total_cost_usd"]) if "total_cost_usd" in r else "-"
        secs = int(r.get("duration_ms") or 0) // 1000
        out.append(f"| {n} | {rc} | {r.get('num_turns', '?')} | {secs} s | {cost} |")
    out += ["", f"## {s.ai.name}", "", *(ai.report(total) if total else ["No result: no session finished."])]
    (d / ai.REPORT).write_text("\n".join(out) + "\n")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # the session lines `status` reads (SESSION_LINE)
