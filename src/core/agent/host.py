"""Commands on the agent computer itself, for either agent's steps: `run` (core.shell's) and `lib`."""

import shlex
from pathlib import Path

from core.computer import LIB
from core.shell import run

__all__ = ["HOME", "lib", "q", "run"]

HOME = Path.home()


def lib(cmd: str, *, env: dict[str, str] | None = None) -> str:
    """A command with `lib.sh`'s functions (apt_install, fetch_checked, install_java...), the ones the other
    computers' scripts use: one way to install things, in bash, for all of them."""
    return run(["bash", "-c", f"source ~/{LIB} && {cmd}"], env=env)


def q(*args: str | Path) -> str:
    return " ".join(shlex.quote(str(a)) for a in args)
