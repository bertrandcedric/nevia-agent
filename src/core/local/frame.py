"""An agent as your machine sees it, both agents' frame: its settings and workspace, what makes its computer
(`bootstrap`), and the commands that are only that (`setup`, `destroy`). The rest runs on the agent computer,
through `agent <agent> remote` (`core.agent.frame`)."""

import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Self

import typer

from core.computer import Computer
from core.config import AgentSettings
from core.local.bootstrap import Bootstrap, Key, Wanted
from core.nevia import Nevia, NeviaError
from core.shell import console, show

Yes = Annotated[bool, typer.Option("--yes", "-y", help="Don't ask first.")]


@dataclass
class Local[S: AgentSettings](ABC):
    settings: S
    nevia: Nevia

    @classmethod
    def of(cls, s: S) -> Self:
        """The agent of these settings, in their workspace."""
        return cls(s, Nevia(s.nevia.workspace))

    @property
    def name(self) -> str:
        return self.settings.agent.computer

    @abstractmethod
    def computer(self) -> Computer: ...

    def wanted(self) -> list[Wanted]:
        """The secrets from `.env` it wants besides the gateway key's (`Bootstrap.of`)."""
        return []

    def other_keys(self) -> list[Key]:
        """The private-key principals it wants besides the agent's own (`Bootstrap.of`)."""
        return []

    def bootstrap(self) -> Bootstrap:
        return Bootstrap.of(self.settings, self.computer(), self.wanted(), self.other_keys())

    def operations(self) -> list[str]:
        """Nevia's operations still running in the workspace (`nevia ops`), a line each: its kind, its resource
        and since when (your clock); none when there is none."""
        ops = self.nevia.operations(running=True)
        if not ops:
            return []
        names = {r["id"]: r["name"] for r in [*self.nevia.computers.list(), *self.nevia.databases.list()]}
        return [
            f"running on Nevia: {o['type']} of {o['resource_type']} {names.get(o['resource'], o['resource'])}, "
            f"since {datetime.fromisoformat(o['created_at']).astimezone():%Y-%m-%d %H:%M:%S}"
            for o in ops
        ]

    def show_operations(self) -> None:
        """`operations`, printed; nothing when there is none."""
        show("\n".join(self.operations()))

    @contextmanager
    def asleep_after(self, job: str) -> Generator[None]:
        """The agent computer put to sleep after what this wraps (following a run, collecting it), however that
        ends, but for a Ctrl-C (the run goes on) or with `job` still running; every command wakes it again."""
        interrupted = False
        try:
            yield
        except KeyboardInterrupt:
            interrupted = True
            raise
        finally:
            if not interrupted:
                self.sleep_unless_running(job)

    def sleep_unless_running(self, job: str) -> None:
        """Asleep, unless `job` still runs there. A failure only says so: the run's own outcome comes first."""
        pc = self.computer()
        try:
            if pc.job_running(job):
                console.log(f"{self.name}: {job} still running, left awake")
            else:
                pc.sleep()
        except (NeviaError, RuntimeError) as err:
            console.log(f"[yellow]{self.name} not put to sleep:[/] {err}")


def setup_command(app: typer.Typer, agent: Callable[[], Local], doc: str) -> None:
    """`setup` on an agent's commands: `Bootstrap.setup`, with `doc` as its help."""

    @app.command("setup", help=doc)
    def setup() -> None:
        agent().bootstrap().setup(dict(os.environ))


def destroy_command(app: typer.Typer, agent: Callable[[], Local], doc: str) -> None:
    """`destroy` on an agent's commands: `Bootstrap.destroy`, with `doc` as its help."""

    @app.command("destroy", help=doc)
    def destroy(yes: Yes = False) -> None:
        agent().bootstrap().destroy(yes=yes)
