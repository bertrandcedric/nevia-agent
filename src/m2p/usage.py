"""The tokens an agent run used, from Copilot's session logs, and what they cost at `copilot.price_*`.

`steps.loop` keeps each session in `logs/session-N.log`, Copilot's `--output-format json` events
rendered as text by tools/render.jq. Copilot reports a model request as an `assistant.usage` event,
which becomes a `usage:` line, and a session's totals per model in `modelMetrics` when it shuts down, a
`usage total:` line. The totals count only for a session with no `usage:` line, so nothing is counted
twice.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from m2p.config import Copilot


@dataclass
class Usage:
    requests: int = 0
    input: int = 0  # cached tokens included, as the OpenAI APIs count them
    cached: int = 0
    output: int = 0

    def merge(self, other: Usage) -> None:
        self.requests += other.requests
        self.input += other.input
        self.cached += other.cached
        self.output += other.output

    def cost(self, prices: Copilot) -> float:
        """US dollars at the prices per million tokens in `[copilot]`."""
        fresh = max(self.input - self.cached, 0)
        return (
            fresh * prices.price_input + self.cached * prices.price_cached_input + self.output * prices.price_output
        ) / 1e6


# render.jq's `usage:` and `usage total:` lines, e.g. `19:21:31 usage: model=m input=9 cached=0 output=2`.
_USAGE = re.compile(r"^(?:\S+ )?usage( total)?: (.*)$")


def session(path: Path, default_model: str) -> dict[str, Usage]:
    """One session's usage by model: its requests' `usage:` lines, or else its `usage total:` lines."""
    per_request: dict[str, Usage] = {}
    totals: dict[str, Usage] = {}
    for line in path.read_text(errors="replace").splitlines():
        if not (m := _USAGE.match(line)):
            continue
        fields = dict(kv.split("=", 1) for kv in m[2].split() if "=" in kv)
        n = {k: int(v) for k, v in fields.items() if v.isdigit()}
        u = Usage(n.get("requests", 1), n.get("input", 0), n.get("cached", 0), n.get("output", 0))
        (totals if m[1] else per_request).setdefault(fields.get("model") or default_model, Usage()).merge(u)
    return per_request or totals


def run(logs: Path, default_model: str) -> dict[str, dict[str, Usage]]:
    """Each session's usage by model, for the `session-N.log` files in `logs`."""
    files = list(logs.glob("session-*.log"))
    files.sort(key=lambda p: int(p.stem.split("-")[-1] or 0))
    return {p.stem: session(p, default_model) for p in files}


def total(sessions: dict[str, dict[str, Usage]]) -> dict[str, Usage]:
    out: dict[str, Usage] = {}
    for by_model in sessions.values():
        for model, u in by_model.items():
            out.setdefault(model, Usage()).merge(u)
    return out
