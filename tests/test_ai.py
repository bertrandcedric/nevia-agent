import json
from pathlib import Path

from core.agent import ai
from core.config import Ai
from m2p import config

AI = config.load().ai
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
    assert ai.render(AI) in tools and ai.render(COPILOT) in tools and b'include "common"' in tools[ai.render(AI)]
