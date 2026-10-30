import json
from pathlib import Path

from core.agent import ai
from core.config import Ai
from core.results import cost_suffix
from m2p.config import Settings

AI = Settings.load().ai
COPILOT = AI.model_copy(update={"engine": "copilot"})


def test_claude_is_the_default_engine() -> None:
    assert Ai.model_fields["engine"].default == "claude"
    assert AI.engine == "claude" and AI.name == "Claude Code"


def test_claude_with_tools_allowed_or_all() -> None:
    cmd = ai.command(AI, "p", home=Path("/h"), allowed=["Read", "Bash(mvn:*)"], dirs=(Path("/tmp"),))  # noqa: S108
    assert cmd[:3] == ["/h/.local/bin/claude", "-p", "p"]
    assert cmd[cmd.index("--tools") + 1] == "Bash,Read"
    assert cmd[cmd.index("--add-dir") + 1] == "/tmp"  # noqa: S108
    assert "--dangerously-skip-permissions" in ai.command(AI, "p", home=Path("/h"))


def test_copilot_allows_all_and_takes_its_settings_from_the_env() -> None:
    cmd = ai.command(COPILOT, "p", home=Path("/h"), allowed=["Read"])
    assert cmd[0] == "/h/.local/bin/copilot" and "--allow-all-tools" in cmd and "--tools" not in cmd
    env = ai.env(COPILOT, "M2P_KEY")
    assert env["COPILOT_PROVIDER_API_KEY"] == "{{secrets.M2P_KEY}}"
    assert env["COPILOT_PROVIDER_BASE_URL"] == COPILOT.gateway_url


def test_copilot_result_is_in_claude_codes_shape() -> None:
    events = [
        json.dumps({"type": "assistant.message", "data": {"content": "looking"}}),
        "some stderr",
        json.dumps({"type": "assistant.message", "data": {"content": "<title>T</title><body>B</body>"}}),
        json.dumps({"type": "result", "exitCode": 0, "usage": {"sessionDurationMs": 5000}}),
    ]
    assert ai.result(COPILOT, events) == {
        "is_error": False, "subtype": "success", "num_turns": 2, "result": "<title>T</title><body>B</body>",
        "duration_ms": 5000,
    }  # fmt: skip
    assert ai.result(COPILOT, events[:2]) is None
    claude = json.dumps({"type": "result", "subtype": "success", "result": "x", "total_cost_usd": 1})
    assert ai.result(AI, ["junk", claude]) == json.loads(claude)


def test_both_renders_are_bundled() -> None:
    tools = ai.tools()
    assert ai.render(AI) in tools and ai.render(COPILOT) in tools and b'include "common"' in tools[ai.render(AI)][0]
    assert {mode for _, mode in tools.values()} == {"444"}  # read-only


def test_a_session_keeps_its_result_next_to_its_log(tmp_path: Path, monkeypatch) -> None:
    end = {"type": "result", "subtype": "success", "result": "x", "total_cost_usd": 1.234}

    def session(cmd, *, cwd, env, render, log) -> int:
        log.with_suffix(".jsonl").write_text(f"junk\n{json.dumps(end)}\n")
        return 3

    monkeypatch.setattr(ai, "session", session)
    log = tmp_path / "ai-1.log"
    assert ai.run_session(AI, ["x"], cwd=tmp_path, env={}, render=tmp_path / "r.jq", log=log) == (3, end)
    assert json.loads(log.with_suffix(".json").read_text()) == end
    monkeypatch.setattr(ai, "session", lambda cmd, *, cwd, env, render, log: 1)
    log = tmp_path / "ai-2.log"
    log.with_suffix(".jsonl").write_text("stderr only\n")
    assert ai.run_session(AI, ["x"], cwd=tmp_path, env={}, render=tmp_path / "r.jq", log=log) == (1, None)
    assert not log.with_suffix(".json").exists()


def test_the_cost_of_a_log_line_has_two_decimals_and_none_without_it() -> None:
    assert cost_suffix({"total_cost_usd": 1.234}) == ", $1.23"
    assert cost_suffix({"total_cost_usd": 2.468}, "Claude Code") == ", Claude Code $2.47"
    assert cost_suffix({"num_turns": 1}, "GitHub Copilot CLI") == cost_suffix(None) == ""


def test_total_sums_the_sessions_and_keeps_the_last_end() -> None:
    a = {"subtype": "error_max_turns", "is_error": True, "num_turns": 2, "total_cost_usd": 1.0,
         "modelUsage": {"m": {"inputTokens": 1, "costUSD": 1.0}}}  # fmt: skip
    b = {"subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.5, "result": "r",
         "modelUsage": {"m": {"inputTokens": 2, "costUSD": 0.5}, "n": {"outputTokens": 4}}}  # fmt: skip
    t = ai.total([a, b])
    assert (t["subtype"], t["result"], t["num_turns"], t["total_cost_usd"], t["sessions"]) == (
        "success",
        "r",
        5,
        1.5,
        2,
    )
    assert t["modelUsage"]["m"]["inputTokens"] == 3 and t["modelUsage"]["n"]["outputTokens"] == 4
    assert ai.total([]) == {"sessions": 0}
    assert "total_cost_usd" not in ai.total([{"subtype": "success", "num_turns": 1}])  # Copilot CLI's: no cost
