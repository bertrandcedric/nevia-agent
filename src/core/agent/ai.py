"""The AI agent both agents run on their computer, `[ai] engine`: Claude Code or GitHub Copilot CLI, through the
Aiven AI gateway. How it is installed, the env of the job that runs it (the gateway's key as a secret template),
its command line, how its events are rendered (core/scripts/render-<engine>.jq) and its result, in one shape."""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from core.agent.host import lib, q, run
from core.computer import bundled, secret
from core.config import Ai
from core.shell import console

BIN = ".local/bin"  # its binary's directory, under its user's home
TOOLS = ("common.jq", "render-claude.jq", "render-copilot.jq")  # in core/scripts, for the agents' tools directory
ENV_PREFIXES = ("ANTHROPIC_", "COPILOT_")  # its settings in a job's env, which a step passes on to it
# A run's files, both agents' alike: each AI session's in logs/ (`log`), and at the run's root its result over
# all of them (`total`) and its report.
RESULT, REPORT = "result.json", "report.md"
USAGE = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "costUSD")


def binary(ai: Ai) -> str:
    return ai.engine  # `claude` or `copilot`, in BIN


def render(ai: Ai) -> str:
    return f"render-{ai.engine}.jq"


def log(run_dir: Path, n: int) -> Path:
    """Session `n`'s rendered events, from 1: its raw events are next to it (`.jsonl`), its result too (`.json`)."""
    return run_dir / "logs" / f"ai-{n}.log"


def tools() -> dict[str, bytes]:
    return {name: bundled("core", f"scripts/{name}") for name in TOOLS}


def install(ai: Ai, computer: str, user: str = "") -> str:
    """The engine in `user`'s BIN (this user's when empty), unless the version wanted is there; its version."""

    def as_user(cmd: str) -> str:
        return run(["sudo", "-n", "-u", user, "-H", "bash", "-lc", cmd] if user else ["bash", "-lc", cmd])

    exe = f"~/{BIN}/{binary(ai)}"
    have = re.search(r"[0-9][0-9.]*", as_user(f"{exe} --version 2>/dev/null || true"))
    if ai.engine == "claude":
        c = ai.claude
        if not have or (c.version not in {"stable", "latest"} and have[0] != c.version):
            as_user(f"curl -fsSL https://claude.ai/install.sh | bash -s {q(c.version)} >/dev/null")
        if not c.gateway_url:
            login = f"sudo -iu {user} claude" if user else "claude"
            console.log(f"log Claude Code in once, if not done: nevia shell {computer}, then {login}, and /login")
    elif not have or have[0] != ai.copilot.version:
        # Fetched and checked by this user (lib.sh's fetch_checked), unpacked by the engine's.
        c = ai.copilot
        url = f"https://registry.npmjs.org/@github/copilot-linux-x64/-/copilot-linux-x64-{c.version}.tgz"
        tgz = run(["mktemp"]).strip()
        try:
            lib(f"fetch_checked {q(url, c.integrity, tgz)} && chmod 644 {q(tgz)}")
            as_user(f"mkdir -p ~/{BIN} && tar -xzf {q(tgz)} -C ~/{BIN} --strip-components 1 package/copilot")
        finally:
            Path(tgz).unlink(missing_ok=True)
    return as_user(f"{exe} --version").strip().splitlines()[0]


def env(ai: Ai, key_secret: str) -> dict[str, str]:
    """The settings of the job that runs the engine: the gateway, and its key as the template of the workspace
    secret `key_secret`."""
    if ai.engine == "copilot":
        c = ai.copilot
        return {
            "COPILOT_OFFLINE": "true",
            "COPILOT_MODEL": c.model,
            "COPILOT_PROVIDER_BASE_URL": c.gateway_url,
            "COPILOT_PROVIDER_API_KEY": secret(key_secret),
            "COPILOT_PROVIDER_WIRE_API": c.wire_api,
            "COPILOT_PROVIDER_MAX_PROMPT_TOKENS": str(c.max_prompt_tokens),
            "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS": str(c.max_output_tokens),
        }
    c = ai.claude
    return {"ANTHROPIC_BASE_URL": c.gateway_url, "ANTHROPIC_AUTH_TOKEN": secret(key_secret)} if c.gateway_url else {}


def command(
    ai: Ai, prompt: str, *, home: Path, allowed: list[str] | None = None, dirs: tuple[Path, ...] = ()
) -> list[str]:
    """The engine headless on `prompt`, its events as JSON lines on stdout. Claude Code may use `allowed` without
    asking (all, when None) and `dirs` besides the working directory; Copilot CLI may use all, everywhere."""
    exe = str(home / BIN / binary(ai))
    if ai.engine == "copilot":
        c = ai.copilot
        return [exe, "-p", prompt, "--allow-all-tools", "--allow-all-paths", "--output-format", "json",
                "--reasoning-effort", c.reasoning_effort]  # fmt: skip
    c = ai.claude
    if allowed is None:
        permissions = ["--dangerously-skip-permissions"]
    else:
        names = ",".join(sorted({t.split("(", 1)[0] for t in allowed}))
        permissions = ["--permission-mode", "acceptEdits", "--tools", names, "--allowedTools", ",".join(allowed)]
    return [
        exe, "-p", prompt, "--output-format", "stream-json", "--verbose", "--max-turns", str(c.max_turns),
        "--effort", c.reasoning_effort, *(["--model", c.model] if c.model else []), *permissions,
        *(arg for d in dirs for arg in ("--add-dir", str(d))),
    ]  # fmt: skip


def session(cmd: list[str], *, cwd: Path, env: dict[str, str], render: Path, log: Path) -> int:
    """`cmd` (`command`'s, maybe wrapped) in `cwd`: its JSON events, its stderr with them, rendered by `render`
    (`tools/render-<engine>.jq`) into `log` as they come, and kept as they are next to it (`.jsonl`); its exit
    code."""
    with log.open("a") as out, log.with_suffix(".jsonl").open("w") as raw:
        jq = subprocess.Popen(["jq", "--unbuffered", "-nRr", "-L", str(render.parent), "-f", str(render)],
                              stdin=subprocess.PIPE, stdout=out, text=True)  # fmt: skip
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)  # fmt: skip
        if not (p.stdout and jq.stdin):
            raise RuntimeError(f"no pipe to {cmd[0]} or jq")
        for line in p.stdout:
            raw.write(line)
            jq.stdin.write(line)
            jq.stdin.flush()
        jq.stdin.close()
        rc = p.wait()
        jq.wait()
    return rc


def result(ai: Ai, events: list[str]) -> dict[str, Any] | None:
    """The session's end, as Claude Code's result event has it (`is_error`, `subtype`, `num_turns`, `result`: its
    last reply, and Claude's own counts), or None without one."""
    parsed = []
    for line in events:
        try:
            if isinstance(e := json.loads(line), dict):
                parsed.append(e)
        except ValueError:
            continue  # stderr
    ends = [e for e in parsed if e.get("type") == "result"]
    if not ends:
        return None
    if ai.engine == "claude":
        return ends[-1]
    replies = [d.get("content") or "" for e in parsed if e.get("type") == "assistant.message"
               and isinstance(d := e.get("data"), dict)]  # fmt: skip
    code = ends[-1].get("exitCode")
    return {
        "is_error": code != 0, "subtype": "success" if code == 0 else f"exit {code}", "num_turns": len(replies),
        "result": next((r for r in reversed(replies) if r.strip()), ""),
        "duration_ms": (ends[-1].get("usage") or {}).get("sessionDurationMs", 0),
    }  # fmt: skip


def total(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Sessions' results as one, in `result`'s shape: the last one's end and reply, their turns, times, cost and
    tokens summed, and `sessions`, how many."""
    out: dict[str, Any] = dict(results[-1]) if results else {}
    for k in ("num_turns", "duration_ms", "duration_api_ms", "total_cost_usd"):
        if any(k in r for r in results):
            out[k] = sum(r.get(k) or 0 for r in results)
    models: dict[str, dict[str, float]] = {}
    for r in results:
        for model, u in (r.get("modelUsage") or {}).items():
            m = models.setdefault(model, {})
            for k in USAGE:
                m[k] = m.get(k, 0) + (u.get(k) or 0)
    if models:
        out["modelUsage"] = models
    return out | {"sessions": len(results)}


def usd(x: Any) -> str:
    return f"${float(x or 0):.4f}"


def report(res: dict[str, Any]) -> list[str]:
    """A result's lines in a run's report: its cost (Claude Code's), turns and times, and its tokens by model."""
    out = []
    if "total_cost_usd" in res:
        out.append(f"- cost: **{usd(res['total_cost_usd'])}** (Claude Code's estimate, at list prices: a "
                   "gateway bills its own)")  # fmt: skip
    ended = f", ended with {res.get('subtype')}" if res.get("is_error") else ""
    secs, api = int(res.get("duration_ms") or 0) // 1000, int(res.get("duration_api_ms") or 0) // 1000
    out += [f"- {res.get('num_turns', '?')} turns, {secs} s (API {api} s){ended}", ""]
    if res.get("modelUsage"):
        out += ["| Model | Input | Output | Cache read | Cache write | Cost |", "|---|---:|---:|---:|---:|---:|"]
    for model, u in (res.get("modelUsage") or {}).items():
        tokens = [u.get(k, 0) for k in USAGE[:-1]]
        out.append(f"| `{model}` | {' | '.join(str(int(n)) for n in tokens)} | {usd(u.get('costUSD'))} |")
    return out
