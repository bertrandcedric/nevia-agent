"""m2p's work on the agent computer that isn't the loop's own logic (`m2p.agent`): the computer's setup, and the
AI sessions themselves, as `agent m2p remote step loop` runs them (a job: the gateway key is in its env)."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from core.agent import ai
from core.agent.host import HOME, lib, run
from core.shell import console
from m2p.config import Settings

SESSION_LOGS = "logs"  # `logs/session-N.log` in LOOP, one per AI session, and its events in session-N.jsonl


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
    `logs/session-N.log`: its start and end lines, and its events as tools/render-<engine>.jq renders them, as
    they come. The gateway key is in this step's env, for the AI only."""
    d = loop_dir(s)
    for f in (f"tools/{ai.render(s.ai)}", "tools/common.jq", "AGENTS.md"):
        if not (d / f).is_file():
            raise RuntimeError(f"{f} is missing: run agent m2p setup")
    cmd = ai.command(s.ai, (d / "AGENTS.md").read_text(), home=HOME)  # the guide is each session's prompt
    env = os.environ | {"PATH": f"{HOME}/.local/bin:{os.environ.get('PATH', '')}"}
    for n in range(1, sessions + 1):
        if (d / "DONE.md").exists():
            break
        log = d / SESSION_LOGS / f"session-{n}.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text(f"{_now()} session {n} started\n")
        rc = ai.session(cmd, cwd=d, env=env, render=d / "tools" / ai.render(s.ai), log=log)
        with log.open("a") as out:
            out.write(f"{_now()} session {n} ended with {rc}\n")
    return "done" if (d / "DONE.md").exists() else f"no DONE.md after {sessions} sessions"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # the session lines `status` reads (SESSION_LINE)
