"""`.env` at the repository root: the secrets the agents need (AIVEN_AI_API_KEY, GITHUB_TOKEN, NEVIA_TOKEN...),
kept out of git. `.env.example` lists them."""

import os
import re
from pathlib import Path

from core.config import ROOT

ENV_FILE = ROOT / ".env"
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse(text: str) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and comments skipped, one pair of surrounding quotes removed."""
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not (m := _LINE.match(line)):
            raise ValueError(f"{ENV_FILE.name}: not KEY=VALUE: {line.split('=', 1)[0]!r}")  # never the value
        key, value = m.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def load(path: Path = ENV_FILE) -> list[str]:
    """Put the file's variables in the environment, unless already set there; returns the names set."""
    if not path.is_file():
        return []
    added = []
    for key, value in parse(path.read_text()).items():
        if key not in os.environ and value:
            os.environ[key] = value
            added.append(key)
    return added
