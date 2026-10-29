"""What runs where: the agent computer's code never needs your machine's (`core.local`, the agents' `local`)."""

import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).parents[1] / "src"


def test_the_agent_side_imports_nothing_of_your_machine() -> None:
    code = (
        "import sys, m2p.agent, m2p.steps, acc.agent, acc.steps, acc.stack, acc.pr, core.agent.build, core.agent.host\n"
        "local = sorted(m for m in sys.modules if m.startswith('core.local') or m.endswith('.local'))\n"
        "print(','.join(local))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], env={"PYTHONPATH": str(SRC)}, capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""
