"""acc's settings: the shared sections of `agent.toml`, and its `[acc]` table."""

from pathlib import Path
from typing import Annotated, ClassVar

from pydantic import StringConstraints

from core.config import CONFIG_FILE, AgentComputer, AgentSettings, Ai, Mongo2pg, MongoDB, Nevia, Runtime, Section, read

# Names that end up in shell commands and Nevia names.
Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]*$")]
# The Unix user the AI runs as on the agent computer: the orchestrator's env and secrets are out of its reach; it
# gets the nevia CLI with a copy of the agent's key (steps.lend_nevia).
AI_USER = "acc-ai"


class Repo(Section):
    token_secret: ClassVar[str] = "GITHUB_TOKEN"  # noqa: S105 - a name; from .env, into ACC_GITHUB_TOKEN

    github: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    base_branch: str

    @property
    def name(self) -> str:
        return self.github.split("/", 1)[1]


class App(Section):
    computer: Name
    port: int
    jdk: int
    maven: str


class Mongo(Section):
    user: ClassVar[str] = "admin"
    password_secret: ClassVar[str] = "MONGO_ROOT_PASSWORD"  # noqa: S105 - a name; random, made by setup

    computer: Name
    database: Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]*$")]


class Chisel(Section):
    user: ClassVar[str] = "tunnel"
    password_secret: ClassVar[str] = "CHISEL_PASSWORD"  # noqa: S105 - a name; random, made by setup

    version: str


class Postgres(Section):
    secret: ClassVar[str] = "DATABASE_URL"  # noqa: S105 - a name: its URI's; a sandbox's is <secret>_<run>

    name: Name


class Agent(AgentComputer):
    computer: Name
    dir: Name
    allowed_tools: list[str]  # what Claude Code may use without asking


class Settings(AgentSettings):
    AGENT = "acc"
    NAMES = ("app.computer", "mongo.computer", "postgres.name", "agent.computer")

    nevia: Nevia
    runtime: Runtime
    ai: Ai
    mongodb: MongoDB
    repo: Repo
    app: App
    mongo: Mongo
    chisel: Chisel
    postgres: Postgres
    mongo2pg: Mongo2pg
    agent: Agent


def load(path: Path = CONFIG_FILE) -> Settings:
    return Settings.model_validate(read("acc", path))
