"""m2p's settings: the shared sections of `agent.toml`, and its `[m2p]` table."""

import re
from typing import ClassVar, Self

from pydantic import model_validator

from core.config import AgentComputer, AgentSettings, Ident, Section
from m2p.usecase import Seed


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

    mongo: Mongo
    seed: Seed
    postgres: Postgres
    agent: Agent

    @property
    def tries(self) -> str:
        """The prefix of the agent's branches of `postgres.name`, one per try (`tools/branch.sh N`: `<tries>N`)."""
        return f"{self.prefix}try"

    def is_try(self, name: str) -> bool:
        """Whether `name` is a try's branch: `<tries>N`, nothing after the number."""
        return re.fullmatch(re.escape(self.tries) + r"[0-9]+", name) is not None

    @property
    def seed_computer(self) -> str:
        """The computer that keeps MongoDB seeded with `[m2p.seed]`: `mongo.computer` is a fresh branch of it."""
        return f"{self.mongo.computer}-seed"

    @property
    def loaded(self) -> str:
        """The copy of `postgres.name` once a run passed, a restore point: never a try's name (`is_try`)."""
        return f"{self.postgres.name}-loaded"

    @model_validator(mode="after")
    def _apart(self) -> Self:
        """The seed computer is not the agent's, and the target no try's (its copy, `-loaded`, can't be one)."""
        if self.seed_computer == self.agent.computer:
            raise ValueError(f"m2p.agent.computer = {self.agent.computer!r} is the seed computer's name")
        if self.is_try(self.postgres.name):
            raise ValueError(f"m2p.postgres.name = {self.postgres.name!r} would be taken for a try's branch")
        return self
