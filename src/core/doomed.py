"""What `clean` or `destroy` deletes, for either agent: listed first, as JSON between the agent computer and your
machine, then deleted in this order (a sandbox before the checkpoint it was restored from, a branch before its
database)."""

import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Doomed:
    computers: list[str] = field(default_factory=list)
    checkpoints: list[tuple[str, str]] = field(default_factory=list)  # computer, checkpoint name
    databases: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    principals: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)  # on the agent computer, under its home
    jobs: str = ""  # a glob of the job logs and exit codes to delete with the dirs

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
        )  # fmt: skip

    def lines(self) -> list[str]:
        """What it deletes, a line per kind, for a person to read before saying yes."""
        shown = {
            "computers": self.computers, "checkpoints": [f"{k} of {c}" for c, k in self.checkpoints],
            "databases": self.databases, "secrets": self.secrets, "principals": self.principals,
            "files": [f"~/{d}" for d in self.dirs],
        }  # fmt: skip
        return [f"{kind}: {', '.join(items)}" for kind, items in shown.items() if items]

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Doomed:
        d = json.loads(text.strip().splitlines()[-1])  # the last line: the command may log before it
        return cls(
            d.get("computers", []), [(c, k) for c, k in d.get("checkpoints", [])], d.get("databases", []),
            d.get("secrets", []), d.get("principals", []), d.get("dirs", []), d.get("jobs", ""),
        )  # fmt: skip
