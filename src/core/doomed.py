"""What `clean` or `destroy` deletes, for either agent: listed first, as JSON between the agent computer and your
machine, then deleted in this order (a sandbox before the checkpoint it was restored from, a branch before its
database)."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import TypeAdapter

from core.nevia import Nevia
from core.shell import console

SUFFIXED = re.compile(r"-\d{12}$")  # a run's computer, `<name>-<run>`: maybe restored from one of the checkpoints


@dataclass(frozen=True)
class Doomed:
    computers: list[str] = field(default_factory=list)
    checkpoints: list[tuple[str, str]] = field(default_factory=list)  # computer, checkpoint name
    databases: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    principals: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)  # on the agent computer, under its home (m2p's clean: reset)
    jobs: str = ""  # a glob of the job logs and exit codes to delete with the dirs
    unprotected: list[str] = field(default_factory=list)  # of the databases, those whose protection is cleared first

    @property
    def on_nevia(self) -> bool:
        """Whether it deletes a Nevia resource, not only files."""
        return bool(self.computers or self.checkpoints or self.databases or self.secrets or self.principals)

    def __bool__(self) -> bool:
        return self.on_nevia or bool(self.dirs)

    def __add__(self, other: Doomed) -> Doomed:
        return Doomed(
            [*self.computers, *other.computers], [*self.checkpoints, *other.checkpoints],
            [*self.databases, *other.databases], [*self.secrets, *other.secrets],
            [*self.principals, *other.principals], [*self.dirs, *other.dirs], self.jobs or other.jobs,
            [*self.unprotected, *other.unprotected],
        )  # fmt: skip

    def lines(self) -> list[str]:
        """What it deletes, a line per kind, for a person to read before saying yes."""
        shown = {
            "computers": self.computers, "checkpoints": [f"{k} of {c}" for c, k in self.checkpoints],
            "databases": self.databases, "protection cleared first": self.unprotected, "secrets": self.secrets,
            "principals": self.principals, "files": [f"~/{d}" for d in self.dirs],
        }  # fmt: skip
        return [f"{kind}: {', '.join(items)}" for kind, items in shown.items() if items]

    def to_json(self) -> str:
        return _DOOMED.dump_json(self).decode()

    @classmethod
    def from_json(cls, text: str) -> Doomed:
        """What `to_json` gave, by field name: a field it lacks takes its default."""
        return _DOOMED.validate_json(text)


_DOOMED = TypeAdapter(Doomed)


def delete(nevia: Nevia, d: Doomed, *, files: Callable[[Doomed], None] | None = None) -> None:
    """Delete what `d` lists, in this order: a run's computers before the checkpoints they were restored from,
    then the files (`files` deletes them, the agent's own way), the other computers, the databases (a branch
    first, as the lists have them, each protected one unprotected just before), the secrets and the principals."""
    suffixed = [c for c in d.computers if SUFFIXED.search(c)]
    for name in suffixed:
        nevia.computers.delete(name)
        console.log(f"deleted computer {name}")
    for computer, checkpoint in d.checkpoints:
        nevia.checkpoints.delete(computer, checkpoint)
        console.log(f"deleted checkpoint {checkpoint} of {computer}")
    if d.dirs and files:
        files(d)
    for name in (c for c in d.computers if c not in suffixed):
        nevia.computers.delete(name)
        console.log(f"deleted computer {name}")
    for name in d.databases:
        if name in d.unprotected:
            nevia.databases.protect(name, False)
            console.log(f"cleared the protection of database {name}")
        nevia.databases.delete(name)
        console.log(f"deleted database {name}")
    for name in d.secrets:
        nevia.secrets.delete(name)
        console.log(f"deleted secret {name}")
    for principal in d.principals:
        nevia.access.remove(principal)
        console.log(f"removed principal {principal}")


def listed_or_deleted(d: Doomed, *, yes: bool, delete: Callable[[Doomed], None]) -> None:
    """The agent's half of `clean` and `destroy`: without `yes`, what it would delete, as JSON, for your machine
    to list and ask about (`Bootstrap.listed_then_deleted`); with it, deleted."""
    if yes:
        delete(d)
    else:
        print(d.to_json())  # noqa: T201 - read by your machine
