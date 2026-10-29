"""m2p's settings: `[nevia]` and `[mongo2pg]` of `agent.toml`, and its `[m2p]` table."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import StringConstraints

from core.config import CONFIG_FILE, AgentSettings, Images, Mongo2pg, Nevia, Runtime, Section, read
from m2p.usecase import Seed

# Names that end up in SQL and shell commands: plain lowercase identifiers only.
Ident = Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]*$")]


class Mongo(Section):
    computer: str
    database: Ident  # also the Postgres database and schema
    cache_gb: float


class Postgres(Section):
    name: str
    secret: str


class Agent(Section):
    computer: str
    dir: str
    hours: float
    sessions: int
    secret: str
    key_path: str


class Copilot(Section):
    version: str
    integrity: str
    gateway_url: str
    gateway_secret: str
    model: str
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    wire_api: str
    max_prompt_tokens: int
    max_output_tokens: int
    price_input: float
    price_cached_input: float
    price_output: float


class Settings(AgentSettings):
    AGENT = "m2p"
    NAMES = ("mongo.computer", "postgres.name", "agent.computer")

    nevia: Nevia
    runtime: Runtime
    images: Images
    mongo: Mongo
    seed: Seed
    postgres: Postgres
    mongo2pg: Mongo2pg
    agent: Agent
    copilot: Copilot


def load(path: Path = CONFIG_FILE) -> Settings:
    return Settings.model_validate(read("m2p", path))
