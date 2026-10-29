"""Local commands, on either side: one way to run them (`run`), their errors, and what prints."""

import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console

console = Console(stderr=True, log_path=False, soft_wrap=True)  # what the agents log as they go
out = Console(soft_wrap=True)  # what a command answers


def show(text: str) -> None:
    """Text as it is (an agent's log, a remote command's output), no markup, no highlighting."""
    if text.strip():
        out.print(text.rstrip(), markup=False, highlight=False)


_CREDENTIALS = re.compile(r"(://[^:/@\s]+:)[^@\s]+@")


def redact(text: str) -> str:
    """Hide passwords in connection URIs that mongo2pg, psql or git print."""
    return _CREDENTIALS.sub(r"\1***@", text)


class CommandError(RuntimeError):
    def __init__(self, args: Sequence[str], code: int, stderr: str) -> None:
        self.args_ = list(args)
        self.code = code
        self.stderr = stderr
        super().__init__(f"{args[0]} exited {code}: {stderr.strip()[-500:]}")


def run(
    args: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, log: Path | None = None
) -> str:
    """`args`, with this process's env and `env`; its output, or a CommandError with its end (URIs scrubbed).
    With `log`, the output goes there (appended) instead, and only its end shows on a failure."""
    full = os.environ | (env or {})
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            done = subprocess.run(list(args), cwd=cwd, env=full, stdout=f, stderr=subprocess.STDOUT, check=False)
        if done.returncode:
            tail = "\n".join(log.read_text(errors="replace").splitlines()[-40:])
            raise CommandError(list(args)[:3], done.returncode, redact(tail))
        return ""
    done = subprocess.run(list(args), cwd=cwd, env=full, capture_output=True, text=True, check=False)
    if done.returncode:
        raise CommandError(list(args)[:3], done.returncode, redact(done.stderr or done.stdout))
    return done.stdout
