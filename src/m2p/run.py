"""A run as both sides know it: the agent's job on its computer, and its status, which the agent sends your
machine as JSON (`agent m2p remote status`)."""

from dataclasses import dataclass, field

from pydantic import TypeAdapter

from core.activity import Activity

AGENT_JOB = "agent"


@dataclass(frozen=True)
class Status:
    running: bool
    done: bool
    sessions: str
    attempts: str
    branches: list[str]
    memory: dict[str, str]
    activity: list[Activity] = field(default_factory=list)


_STATUS = TypeAdapter(Status)  # by field name, as JSON


def status_json(st: Status) -> str:
    """A status as the agent sends it to your machine."""
    return _STATUS.dump_json(st).decode()


def status_of(text: str) -> Status:
    return _STATUS.validate_json(text)
