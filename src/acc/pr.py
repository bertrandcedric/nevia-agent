"""What a run writes: the pull request's text from the AI's reply, and the run's report. `ai` is the AI's name
(`Ai.name`), and its result is in Claude Code's shape (`core.agent.ai.result`)."""

import re
from dataclasses import dataclass, field
from typing import Any

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINKS = {
    "Claude Code": "https://claude.com/claude-code",
    "GitHub Copilot CLI": "https://github.com/features/copilot/cli",
}


def footer(ai: str) -> str:
    return f"\n---\nProposed by {ai} running on a Nevia computer.\n\n🤖 Generated with [{ai}]({LINKS[ai]})\n"


class NoProposal(RuntimeError):
    pass


def proposal(result: dict[str, Any], ai: str) -> tuple[str, str]:
    """The pull request's title and body from the AI's final result: the `<title>` and `<body>` of its reply, or
    else its first line and the rest."""
    if result.get("is_error"):
        why = result.get("result") or result.get("subtype") or "an error"
        hint = " (raise ai.claude.max_turns)" if result.get("subtype") == "error_max_turns" else ""
        raise NoProposal(f"{ai} ended with {why} after {result.get('num_turns', '?')} turns{hint}")
    text = str(result.get("result") or "").strip()

    def tag(name: str) -> str:
        m = re.search(rf"<{name}>([\s\S]*?)</{name}>", text)
        return m[1].strip() if m else ""

    title, body = tag("title"), tag("body")
    if not title:
        first, _, rest = text.partition("\n")
        title, body = first, rest.strip()
    title = " ".join(re.sub(r"^#+\s*", "", title).split())[:120]
    if not title:
        raise NoProposal(f"{ai}'s reply has no title")
    return title, body


def _tail(log: str, n: int) -> str:
    lines = [line for line in ANSI.sub("", log).splitlines() if line.strip()]
    return "\n".join(lines[-n:])


@dataclass
class Validation:
    """What the pipeline checked of the change, for the pull request."""

    stack: str
    base_branch: str
    sandbox: str
    checkpoint: str
    kind: str
    database: str  # how the sandbox got its database
    url: str
    ai: str
    analysis: str = ""  # summary.md of the mongo2pg analysis, for a migration
    feature_check: str = ""  # the AI's script, for a task
    feature_log: str = ""


def body(text: str, v: Validation, run: str) -> str:
    """The AI's text, then what the pipeline did: the analysis, the validation, the feature check, the preview."""
    parts = [text.strip(), ""]
    if v.analysis:
        parts += ["## Data analysis (mongo2pg)", "", f"Given to {v.ai}, read-only, without its sample values.", ""]
        parts += [v.analysis.strip(), ""]
    parts += [
        "## Automated validation",
        "",
        f"- `{v.stack}` recreated from `{v.base_branch}` and checked before the change.",
        "- `mvn package` passes with the change (the tests included).",
        f"- Sandbox `{v.sandbox}` (checkpoint `{v.checkpoint}` of `{v.stack}`, `database.kind`: `{v.kind}`, "
        f"{v.database}): GET /healthz and GET / answer.",
    ]
    if v.feature_check:
        parts += ["", "### Feature check", "", f"{v.ai}'s script, run on the sandbox (a fork of the data):", ""]
        parts += ["<details><summary>feature-check.sh</summary>", "", "```sh", v.feature_check.rstrip(), "```"]
        parts += ["", "</details>", "", "```", _tail(v.feature_log, 30), "```"]
    parts += [
        "",
        "## Preview",
        "",
        f"{v.url}: delete it once the pull request is closed, `agent acc clean --run {run}`.",
    ]
    return "\n".join(parts) + "\n" + footer(v.ai)


@dataclass
class Report:
    run: str
    kind: str
    repo: str
    base_branch: str
    started: str
    outcome: str
    ai: str
    steps: list[tuple[str, float]] = field(default_factory=list)  # name, seconds
    pr_url: str = ""
    sandbox_url: str = ""
    result: dict[str, Any] | None = None
    left: list[str] = field(default_factory=list)


def _dur(seconds: float) -> str:
    s = round(seconds)
    return f"{s // 60}m{s % 60:02d}s"


def _usd(x: Any) -> str:
    return f"${float(x or 0):.4f}"


def report(r: Report) -> str:
    """The run's report: its outcome, the time of each step, the AI's turns (Claude Code's cost) and what it left
    on Nevia."""
    out = [f"# Run {r.run}", "", f"- {r.kind} of `{r.repo}` (`{r.base_branch}`), {r.started}: {r.outcome}"]
    if r.pr_url:
        out.append(f"- pull request: {r.pr_url}")
    if r.sandbox_url:
        out.append(f"- preview: {r.sandbox_url}")
    out += ["", "## Time", "", "| Step | Duration |", "|---|---:|"]
    out += [f"| {name} | {_dur(s)} |" for name, s in r.steps]
    out += [f"| **total** | **{_dur(sum(s for _, s in r.steps))}** |", "", f"## {r.ai}", ""]
    if res := r.result:
        if "total_cost_usd" in res:
            out.append(f"- cost: **{_usd(res['total_cost_usd'])}** (Claude Code's estimate, at list prices: a "
                       "gateway bills its own)")  # fmt: skip
        ended = f", ended with {res.get('subtype')}" if res.get("is_error") else ""
        secs, api = int(res.get("duration_ms") or 0) // 1000, int(res.get("duration_api_ms") or 0) // 1000
        out += [f"- {res.get('num_turns', '?')} turns, {secs} s (API {api} s){ended}", ""]
        if res.get("modelUsage"):
            out += ["| Model | Input | Output | Cache read | Cache write | Cost |", "|---|---:|---:|---:|---:|---:|"]
        for model, u in (res.get("modelUsage") or {}).items():
            tokens = [
                u.get(k, 0) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
            ]
            out.append(f"| `{model}` | {' | '.join(map(str, tokens))} | {_usd(u.get('costUSD'))} |")
    else:
        out.append(f"No result: {r.ai} did not finish (stopped, or the run failed before).")
    out += ["", "## Nevia", "", f"Left by the run (`agent acc clean --run {r.run}` deletes them):", ""]
    out += [f"- {x}" for x in r.left] or ["- nothing"]
    return "\n".join(out) + "\n"
