"""`agent.toml` at the repository root: the sections both agents share, `[nevia]` and `[mongo2pg]`, then one
table per agent, `[m2p]` and `[acc]`, which each agent's own settings read."""

import tomllib
from pathlib import Path
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

ROOT = Path(__file__).resolve().parents[2]
BUILD_DIR = ROOT / "build"  # your machine's results, and on an agent computer mongo2pg's binary and runs' state
CONFIG_FILE = ROOT / "agent.toml"


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Nevia(Section):
    workspace: str
    cli_version: str  # the nevia CLI installed on an agent computer that needs one (m2p's)


class Runtime(Section):
    uv: str
    python: str


class Images(Section):
    mongo: str


class Mongo2pg(Section):
    repo: str
    release: Annotated[str, StringConstraints(min_length=1)]

    @property
    def github(self) -> str:
        """`owner/name` of the repository on GitHub."""
        return self.repo.removeprefix("https://github.com/").removesuffix(".git")


class AgentSettings(Section):
    """An agent's settings. Everything it makes on Nevia carries its name, so the two agents never meet in a
    workspace: resources `<agent>-...` (checked when the settings are read), secrets `<AGENT>_...`
    (`secret`)."""

    AGENT: ClassVar[str]  # m2p or acc
    NAMES: ClassVar[tuple[str, ...]]  # the dotted paths of the Nevia names in these settings

    @property
    def prefix(self) -> str:
        return f"{self.AGENT}-"

    def secret(self, name: str) -> str:
        """The workspace secret `name` names in these settings: `<AGENT>_<name>`."""
        return f"{self.AGENT.upper()}_{name}"

    @model_validator(mode="after")
    def _prefixed(self) -> AgentSettings:
        for path in self.NAMES:
            value: Any = self
            for part in path.split("."):
                value = getattr(value, part)
            if not str(value).startswith(self.prefix):
                raise ValueError(
                    f"{self.AGENT}.{path} = {value!r}: a Nevia name of {self.AGENT} starts with {self.prefix}"
                )
        return self


def read(agent: str, path: Path = CONFIG_FILE) -> dict[str, Any]:
    """One agent's settings: the shared sections, and its own table's."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    shared = {k: data.get(k) for k in ("nevia", "runtime", "images", "mongo2pg")}
    return {**shared, **data.get(agent, {})}
