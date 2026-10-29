"""`agent.toml` at the repository root: the sections both agents share (`[nevia]`, `[runtime]`, `[ai]`,
`[mongodb]`, `[mongo2pg]`), then one table per agent, `[m2p]` and `[acc]`, which each agent's own settings read.
What is a convention, not a choice (secret names, users, paths), is in the code, as ClassVars."""

import tomllib
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

ROOT = Path(__file__).resolve().parents[2]
BUILD_DIR = ROOT / "build"  # your machine's results, and on an agent computer mongo2pg's binary and runs' state
CONFIG_FILE = ROOT / "agent.toml"


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Nevia(Section):
    workspace: str


class Runtime(Section):
    uv: str
    python: str
    nevia: str  # the nevia CLI on the agent computers


SecretName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]


class MongoDB(Section):
    image: str
    cache_gb: float  # WiredTiger's cache: Nevia computers have 2 GiB and no swap


class Claude(Section):
    version: str
    gateway_url: str  # empty: a login on the agent computer
    model: str
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"]  # what Claude Code's --effort takes
    max_turns: int


class Copilot(Section):
    version: str
    integrity: str
    gateway_url: Annotated[str, StringConstraints(min_length=1)]  # Copilot CLI has no login here
    model: str
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    wire_api: str
    max_prompt_tokens: int
    max_output_tokens: int


class Ai(Section):
    """The AI agent both agents run (`core.agent.ai`), and the env var of the gateway's key in `.env`."""

    engine: Literal["claude", "copilot"] = "claude"
    gateway_secret: SecretName
    claude: Claude
    copilot: Copilot

    @property
    def gateway_url(self) -> str:
        return self.claude.gateway_url if self.engine == "claude" else self.copilot.gateway_url

    @property
    def name(self) -> str:
        return "Claude Code" if self.engine == "claude" else "GitHub Copilot CLI"


class Mongo2pg(Section):
    repo: str
    release: Annotated[str, StringConstraints(min_length=1)]

    @property
    def github(self) -> str:
        """`owner/name` of the repository on GitHub."""
        return self.repo.removeprefix("https://github.com/").removesuffix(".git")


class AgentComputer(Section):
    """An agent's own computer, and its Nevia key: the workspace secret `<AGENT>_NEVIA_AGENT_KEY`, then a file
    under the computer user's home."""

    secret: ClassVar[str] = "NEVIA_AGENT_KEY"  # noqa: S105 - a name

    computer: str
    dir: str  # its working directory, under the home
    hours: float  # how long a run's AI may take

    @property
    def key_path(self) -> str:
        return f".config/nevia/keys/{self.computer}.json"


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
    shared = {k: data.get(k) for k in ("nevia", "runtime", "ai", "mongodb", "mongo2pg")}
    return {**shared, **data.get(agent, {})}
