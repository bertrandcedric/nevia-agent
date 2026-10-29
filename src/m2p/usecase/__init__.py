"""The use case: the hello MongoDB of nevia-helloworld, its customers as agent/uc3 seeds them.

What is specific to that data lives here, the rest of the agent loop works on any MongoDB database:
`seed.py` generates the data on the source computer, from the `[m2p.seed]` settings, and `DESCRIPTION`
describes it in the agent's guide.
"""

import ast
import shlex
from importlib import resources

from pydantic import BaseModel, ConfigDict, field_validator

COLLECTION = "customers"  # the source has no data while this collection is empty


def file(name: str) -> bytes:
    return resources.files(__package__).joinpath(name).read_bytes()


def _strange() -> list[str]:
    """seed.py's STRANGE, read from its source: seed.py needs pymongo, which only the source computer has."""
    for node in ast.parse(file("seed.py")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "STRANGE" for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError("seed.py has no STRANGE")


STRANGE = _strange()  # the kinds of anomalies seed.py can add


class Seed(BaseModel):
    """The `[seed]` settings: `seed.py --help` says what each one does."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    customers: int
    seed: int
    strange: float
    # A list of STRANGE's kinds, ["all"] or []; "all", "" and "a,b" are read as those.
    strange_kinds: list[str]

    @field_validator("strange_kinds", mode="before")
    @classmethod
    def _kinds(cls, v: object) -> object:
        if isinstance(v, str):
            v = [k.strip() for k in v.split(",") if k.strip() and k.strip() != "none"]
        if isinstance(v, list) and (unknown := [k for k in v if k != "all" and k not in STRANGE]):
            raise ValueError(f"unknown strange kinds {unknown}; they are: all, {', '.join(STRANGE)}")
        return v


# The data, as the agent's guide (`{data}` in prompts/m2p/AGENTS.md) puts it after the name of its database.
DESCRIPTION = (
    "an app's customers: one collection, `customers`, one document per customer with its addresses, contacts, "
    "orders and their items, and preferences"
)


def seed_args(s: Seed, db: str) -> str:
    return shlex.join([
        "--db", db, "--customers", str(s.customers), "--seed", str(s.seed), "--strange", str(s.strange),
        "--strange-kinds", "all" if "all" in s.strange_kinds else ",".join(s.strange_kinds),
    ])  # fmt: skip
