"""acc's settings: the shared sections of `agent.toml`, and its `[acc]` table."""

from typing import Annotated, ClassVar

from pydantic import StringConstraints

from core.config import AgentComputer, AgentSettings, Ident, Section

# Names that end up in shell commands and Nevia names.
Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]*$")]
# The Unix user the AI runs as on the agent computer: the orchestrator's env and secrets are out of its reach; it
# gets the nevia CLI with a viewer's key of its own (`Agent.ai_principal`, steps.lend_nevia).
AI_USER = "acc-ai"


class Repo(Section):
    token_secret: ClassVar[str] = "GITHUB_TOKEN"  # noqa: S105 - a name; from .env, into ACC_GITHUB_TOKEN

    github: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    base_branch: str


class App(Section):
    computer: Name
    port: int
    jdk: int
    maven: str


class Mongo(Section):
    user: ClassVar[str] = "admin"
    password_secret: ClassVar[str] = "MONGO_ROOT_PASSWORD"  # noqa: S105 - a name; random, made by setup

    computer: Name
    database: Ident


class Chisel(Section):
    user: ClassVar[str] = "tunnel"
    password_secret: ClassVar[str] = "CHISEL_PASSWORD"  # noqa: S105 - a name; random, made by setup

    version: str


class Postgres(Section):
    secret: ClassVar[str] = "DATABASE_URL"  # noqa: S105 - a name: its URI's; a sandbox's is <secret>_<run>
    # JDBC's driver prepares statements on the server after 5 runs, which a pooler in transaction mode (Nevia's)
    # loses between transactions: 0 turns that off.
    pooled_params: ClassVar[dict[str, str]] = {"prepareThreshold": "0"}

    name: Name
    pooler: bool = False  # the app (stack and sandbox) through the database's pooler, port 6432


class Agent(AgentComputer):
    ai_secret: ClassVar[str] = "NEVIA_AI_KEY"  # noqa: S105 - a name: the AI's key file, ACC_NEVIA_AI_KEY

    computer: Name
    dir: Name
    allowed_tools: list[str]  # what Claude Code may use without asking

    @property
    def ai_principal(self) -> str:
        """The AI's own principal, a viewer: it can look at the workspace, not change it, run commands or read a
        database's password. Made and removed by your machine (the workspace's admin only may)."""
        return f"{self.computer}-ai"

    @property
    def ai_key_path(self) -> str:
        """Its key file on the agent computer, under the home: the agent's copy, which `lend_nevia` lends."""
        return f".config/nevia/keys/{self.ai_principal}.json"


class Settings(AgentSettings):
    AGENT = "acc"
    NAMES = ("app.computer", "mongo.computer", "postgres.name", "agent.computer")

    repo: Repo
    app: App
    mongo: Mongo
    chisel: Chisel
    postgres: Postgres
    agent: Agent
