"""The agents' prompts, all in `prompts/<agent>/` at the project's root (next to agent.toml, here and on an agent
computer): what each model is told, apart from the code."""

from core.config import ROOT


def prompt(agent: str, name: str) -> str:
    """`prompts/<agent>/<name>`, e.g. m2p's guide `AGENTS.md` or acc's task `MIGRATE.md`."""
    return (ROOT / "prompts" / agent / name).read_text()
