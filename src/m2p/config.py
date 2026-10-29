"""m2p's settings: the shared sections of `agent.toml`, and its `[m2p]` table."""

from pathlib import Path
from typing import Annotated, ClassVar

from pydantic import StringConstraints

from core.config import CONFIG_FILE, AgentComputer, AgentSettings, Ai, Mongo2pg, MongoDB, Nevia, Runtime, Section, read
from m2p.usecase import Seed

# Names that end up in SQL and shell commands: plain lowercase identifiers only.
Ident = Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]*$")]


class Mongo(Section):
    computer: str
    database: Ident  # also the Postgres database and schema


class Postgres(Section):
    # The workspace secret M2P_PG_URL holds its URI, DATABASE_URL on the agent computer.
    secret: ClassVar[str] = "PG_URL"  # noqa: S105 - a name

    name: str


class Agent(AgentComputer):
    sessions: int  # AI sessions in a run, if one stops before DONE.md


class Settings(AgentSettings):
    AGENT = "m2p"
    NAMES = ("mongo.computer", "postgres.name", "agent.computer")

    nevia: Nevia
    runtime: Runtime
    ai: Ai
    mongodb: MongoDB
    mongo: Mongo
    seed: Seed
    postgres: Postgres
    mongo2pg: Mongo2pg
    agent: Agent


def load(path: Path = CONFIG_FILE) -> Settings:
    return Settings.model_validate(read("m2p", path))
