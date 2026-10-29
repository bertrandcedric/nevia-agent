"""acc's settings: `[nevia]` and `[mongo2pg]` of `agent.toml`, and its `[acc]` table."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import StringConstraints

from core.config import CONFIG_FILE, AgentSettings, Images, Mongo2pg, Nevia, Runtime, Section, read

# Names that end up in shell commands and Nevia names.
Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]*$")]
SecretName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]


class Repo(Section):
    github: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    base_branch: str
    token_secret: SecretName

    @property
    def name(self) -> str:
        return self.github.split("/", 1)[1]


class App(Section):
    computer: Name
    port: int
    jdk: int
    maven: str


class Mongo(Section):
    computer: Name
    database: Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]*$")]
    cache_gb: float
    user: str
    password_secret: SecretName


class Chisel(Section):
    version: str
    user: str
    password_secret: SecretName


class Postgres(Section):
    name: Name
    secret: SecretName


class Agent(Section):
    computer: Name
    dir: Name
    hours: float
    secret: SecretName
    key_path: str


class GatewayModels(Section):
    opus: str = ""
    sonnet: str = ""
    haiku: str = ""


class Claude(Section):
    user: Name
    version: str
    gateway_url: str
    gateway_secret: SecretName
    model: str
    gateway_models: GatewayModels
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"]  # what Claude Code's --effort takes
    max_turns: int
    allowed_tools: list[str]


class Settings(AgentSettings):
    AGENT = "acc"
    NAMES = ("app.computer", "mongo.computer", "postgres.name", "agent.computer")

    nevia: Nevia
    runtime: Runtime
    images: Images
    repo: Repo
    app: App
    mongo: Mongo
    chisel: Chisel
    postgres: Postgres
    mongo2pg: Mongo2pg
    agent: Agent
    claude: Claude


def load(path: Path = CONFIG_FILE) -> Settings:
    return Settings.model_validate(read("acc", path))
