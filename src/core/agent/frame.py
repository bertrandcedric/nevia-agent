"""An agent as its own computer runs it, both agents' frame: its settings and workspace, its computer, and its
tunnels; and the hidden `agent <agent> remote` commands, which your machine's commands (`core.local.frame`) run
there."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Self

import typer
from nevia_kit import Tunnels

from core.computer import Computer
from core.config import AgentSettings
from core.nevia import Nevia


@dataclass
class Here[S: AgentSettings](ABC):
    settings: S
    nevia: Nevia
    poll: float = 10.0
    tunnels: Tunnels = field(init=False)

    def __post_init__(self) -> None:
        self.tunnels = Tunnels(self.nevia.kit, lifetime=self.link_lifetime())

    @classmethod
    def of(cls, s: S, **kwargs: Any) -> Self:
        """The agent of these settings, in their workspace: `agent <agent> remote`'s, signed in with its key."""
        return cls(s, Nevia(s.nevia.workspace), **kwargs)

    def link_lifetime(self) -> int:
        """Seconds a link it makes lasts: a run's AI time (`agent.hours`)."""
        return int(self.settings.agent.hours * 3600)

    @property
    def name(self) -> str:
        return self.settings.agent.computer

    @property
    def dir(self) -> str:
        return self.settings.agent.dir

    @abstractmethod
    def computer(self) -> Computer: ...


def remote_app() -> typer.Typer:
    """An agent's hidden `remote` commands, what its commands do on the agent computer."""
    return typer.Typer(no_args_is_help=True, hidden=True, help="On the agent computer: what the commands do there.")
