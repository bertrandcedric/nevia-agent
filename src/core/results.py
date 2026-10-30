"""A run's result files, both agents' alike, and how their cost reads: the agent computer writes them, your
machine reads them back from the collected run (both sides)."""

from typing import Any

# Each AI session's in logs/ (`core.agent.ai.log`), and at the run's root its result over all of them
# (`core.agent.ai.total`) and its report.
RESULT, REPORT = "result.json", "report.md"


def usd(x: Any) -> str:
    return f"${float(x or 0):.4f}"


def cost_suffix(res: dict[str, Any] | None, name: str = "") -> str:
    """`, [name ]$1.23`, Claude Code's cost of a result for a log line, or nothing without one."""
    if not res or "total_cost_usd" not in res:
        return ""
    return f", {name + ' ' if name else ''}${float(res['total_cost_usd']):.2f}"
