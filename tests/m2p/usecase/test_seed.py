import shlex

import pytest
from bson import ObjectId
from pydantic import ValidationError

from m2p import usecase
from m2p.usecase import seed

# What agent/uc3's seed.mjs makes of SEED=42, 5000 customers, SEED_STRANGE=5: the port has to match it.
UC3_SEEN = {
    "scalar-not-array": 25, "odd-field-names": 25, "object-not-array": 27, "quantity-string": 27, "empty-object": 19,
    "bool-string": 22, "quantity-negative": 18, "unicode": 26, "phone-number": 21, "unknown-fields": 23,
    "quantity-long": 29, "duplicate-order-id": 16, "price-decimal128": 22, "date-epoch": 16, "nested-deeper": 21,
    "mixed-array": 30, "null-fields": 16, "long-text": 26, "empty-strings": 25, "missing-required": 23,
    "price-string": 22, "id-objectid": 21, "enum-case": 24, "date-string": 19,
}  # fmt: skip


def _customers(pct: float = 5) -> tuple[seed.Customers, list[dict]]:
    gen = seed.Customers(42, pct, seed.STRANGE)
    return gen, [gen.next(n) for n in range(1, 5001)]


def test_the_seed_makes_the_customers_of_uc3() -> None:
    gen, docs = _customers()
    assert gen.strange_docs == 267
    assert gen.seen == UC3_SEEN
    first = docs[0]
    assert (first["_id"], first["name"], first["email"], first["status"]) == (
        "C-S00001", "Nathan Leroy", "nathan.leroy.1@mail.example", "active",
    )  # fmt: skip
    # mongo2pg's post-import report of uc3 counts as many orders and contacts in MongoDB.
    assert sum(len(d["orders"]) for d in docs) == 20046
    assert sum(len(d["contacts"]) for d in docs) == 3051


def test_strange_customers_leave_the_others_as_they_are() -> None:
    _, normal = _customers(0)
    gen, strange = _customers(5)
    assert sum(a != b for a, b in zip(normal, strange, strict=True)) <= gen.strange_docs
    assert normal[0] == strange[0]


def test_absent_fields_are_left_out() -> None:
    _, docs = _customers(0)
    assert all(seed.MISSING not in d.values() for d in docs)
    assert any("phone" not in d for d in docs)
    assert not any(isinstance(d["_id"], ObjectId) for d in docs)


class _Collection:
    def __init__(self) -> None:
        self.docs: list[dict] = []

    def insert_many(self, docs: list[dict], ordered: bool) -> None:
        self.docs.extend(docs)

    def count_documents(self, _filter: dict) -> int:
        return len(self.docs)


def _seed(monkeypatch, *args: str) -> list[dict]:
    customers = _Collection()

    class Client:
        def __init__(self, _url: str) -> None:
            pass

        def __enter__(self):
            return self

        def __getitem__(self, db: str) -> dict:
            return {"customers": customers}

        def drop_database(self, db: str) -> None:
            assert db == "hello"
            customers.docs.clear()

        def __exit__(self, *_exc) -> None:
            pass

    monkeypatch.setattr(seed, "MongoClient", Client)
    seed.main(["--customers", "300", *args])
    return customers.docs


def test_no_strange_kinds_means_no_strange_customers(monkeypatch, capsys) -> None:
    normal = _seed(monkeypatch, "--strange", "0")
    for none in ("", "none"):
        assert _seed(monkeypatch, "--strange-kinds", none) == normal
        assert ", 0 strange" in capsys.readouterr().out
    assert _seed(monkeypatch, "--strange-kinds", "date-string", "--strange", "50") != normal


def test_the_log_names_every_kind_asked_for_and_warns_of_one_never_drawn(monkeypatch, capsys) -> None:
    _seed(monkeypatch, "--customers", "5000", "--strange", "0.1", "--strange-kinds", "date-string,bool-string")
    out = capsys.readouterr().out
    assert "[seed]   bool-string: 4" in out
    assert "[seed]   date-string: 0" in out  # 4 strange customers drew bool-string only
    assert "[seed] warning: no customer drew date-string" in out
    _seed(monkeypatch, "--customers", "5000", "--strange", "1", "--strange-kinds", "date-string,bool-string")
    assert "warning" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("kinds", "arg"),
    [
        (["date-string", "bool-string"], "date-string,bool-string"),
        ("date-string,bool-string", "date-string,bool-string"),
        ([], ""), ("", ""), ("none", ""),
        (["all"], "all"), ("all", "all"),
    ],
)  # fmt: skip
def test_strange_kinds_as_a_list_or_a_string(kinds: object, arg: str) -> None:
    s = usecase.Seed(customers=1, seed=42, strange=5, strange_kinds=kinds)
    assert f"--strange-kinds {shlex.quote(arg)}" in usecase.seed_args(s, "hello")


def test_an_unknown_strange_kind_fails_when_the_settings_load() -> None:
    with pytest.raises(ValidationError, match="unknown strange kinds"):
        usecase.Seed(customers=1, seed=42, strange=5, strange_kinds=["date-string", "date-strng"])


def test_the_settings_read_seed_pys_kinds_without_importing_it() -> None:
    assert usecase.STRANGE == seed.STRANGE
