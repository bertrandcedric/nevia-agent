"""The agents' prompts, all in `src/prompts/<agent>/`: what each model is told, apart from the code."""

from importlib import resources


def prompt(agent: str, name: str) -> str:
    """`prompts/<agent>/<name>`, e.g. m2p's guide `AGENTS.md` or acc's task `MIGRATE.md`."""
    return resources.files("prompts").joinpath(agent, name).read_text()
