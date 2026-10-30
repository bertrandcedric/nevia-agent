"""Fill the database hello with the customers of nevia-helloworld, as the app stores them.

One document per customer, with its addresses, contacts, orders and their items, tags and
preferences: optional fields, lists of every length (empty ones included), numbers with and
without decimals. The same --seed gives the same customers, ids C-S00001 and up; the collection is
replaced, so a seed gives exactly --customers of them.

--strange percent of them are strange: one to three anomalies each (STRANGE below), as a database
that several app versions and imports wrote to has them, e.g. a number as a string, an object
where a list should be, an ObjectId for an id. They come from a random stream of their own: the
same seed gives the same normal data whatever the percent.

A port of agent/uc3's seed.mjs: the same seed gives the same documents (but the ObjectIds).
"""

import argparse
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from bson import Decimal128, Int64, ObjectId
from pymongo import MongoClient

MASK = 0xFFFFFFFF
FIRST = ["Claire", "Hugo", "Léa", "Lucas", "Chloé", "Nathan", "Inès", "Théo", "Zoé", "Jules", "Anaïs", "Noé"]
LAST = ["Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit", "Durand", "Leroy", "Moreau"]
COMPANY = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Stark", "Wayne", "Soylent", "Tyrell", "Cyberdyne"]
SECTOR = ["Distribution", "Logistique", "Industries", "Services", "Conseil", "Immobilier"]
CITY = [("Paris", "75004"), ("Lyon", "69002"), ("Marseille", "13001"), ("Lille", "59000"), ("Nantes", "44000"),
        ("Bordeaux", "33000"), ("Toulouse", "31000"), ("Strasbourg", "67000")]  # fmt: skip
STREET = ["rue de Rivoli", "place Bellecour", "avenue Jean Jaurès", "boulevard Haussmann", "quai des Chartrons"]
ROLE = ["Achats", "Comptabilité", "Direction", "Logistique", "Support"]
TAG = ["retail", "vip", "online", "wholesale", "b2b", "loyalty", "new"]
# Prices as the app's JSON has them: 249 is an integer (int32 in BSON), 89.9 a double.
PRODUCT = [("CHA-100", "Chaise de bureau", 89.9), ("BUR-200", "Bureau 160 cm", 249), ("LAM-010", "Lampe LED", 24.5),
           ("LIV-501", "Coffret livres", 59), ("CAR-002", "Carte cadeau", 25), ("ECR-270", 'Écran 27"', 319.99),
           ("CLA-090", "Clavier", 49.95)]  # fmt: skip
ORDER_STATUS = ["pending", "paid", "shipped", "delivered", "cancelled"]
DAY = 86_400_000
START = 1_672_531_200_000  # 2023-01-01T00:00:00Z, in ms
END = 1_751_241_600_000  # 2025-06-30T00:00:00Z
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MISSING = object()  # a field left out, as JavaScript's undefined


def stream(state: int) -> Callable[[], float]:
    """mulberry32, as seed.mjs has it: small and deterministic, floats in [0, 1)."""

    def imul(a: int, b: int) -> int:
        return (a * b) & MASK

    def rand() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & MASK
        t = imul(state ^ (state >> 15), state | 1)
        t ^= (t + imul(t ^ (t >> 7), t | 61)) & MASK
        return ((t ^ (t >> 14)) & MASK) / 4294967296

    return rand


def when(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def ms_of(d: datetime) -> int:
    return (d - EPOCH) // timedelta(milliseconds=1)


def is_number(v: object) -> bool:
    """JavaScript's typeof v === 'number': a Long, a Decimal128 or a bool is not one."""
    return type(v) in {int, float}


def clean(v: Any) -> Any:
    """Absent fields left out, as the app does (absent and null are the same to it)."""
    if isinstance(v, list):
        return [clean(x) for x in v]
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items() if x is not MISSING}
    return v


class Customers:
    def __init__(self, seed: int, strange_pct: float, kinds: list[str]) -> None:
        self.normal = stream(seed & MASK)
        self.strange = stream((seed ^ 0x5EED5) & MASK)
        self.rand = self.normal
        self.strange_pct = strange_pct
        self.kinds = kinds
        self.strange_docs = 0
        self.seen: dict[str, int] = {}

    def between(self, lo: int, hi: int) -> int:
        return lo + int(self.rand() * (hi - lo + 1))

    def pick[T](self, items: list[T]) -> T:
        return items[self.between(0, len(items) - 1)]

    def some[T](self, items: list[T], most: int) -> list[T]:
        n = self.between(0, most)
        return list(dict.fromkeys(self.pick(items) for _ in range(n)))

    def maybe(self, p: float, value: Callable[[], Any]) -> Any:
        return value() if self.rand() < p else MISSING

    def date(self, lo: int, hi: int) -> datetime:
        return when(int(lo + self.rand() * (hi - lo)))

    def customer(self, n: int) -> dict[str, Any]:
        company = self.rand() < 0.4
        first, last = self.pick(FIRST), self.pick(LAST)
        name = f"{self.pick(COMPANY)} {self.pick(SECTOR)}" if company else f"{first} {last}"
        domain = f"{re.sub(r'[^a-z]+', '-', name.lower())}.example" if company else "mail.example"
        email = (
            f"contact@{domain}"
            if company
            else re.sub(r"[\u0300-\u036f]", "", unicodedata.normalize("NFD", f"{first}.{last}.{n}@{domain}".lower()))
        )

        def address(kind: str) -> dict[str, Any]:
            city, postal_code = self.pick(CITY)
            return {"kind": kind, "street": f"{self.between(1, 120)} {self.pick(STREET)}", "city": city,
                    "postalCode": postal_code, "country": "FR"}  # fmt: skip

        def contact() -> dict[str, Any]:
            f, la = self.pick(FIRST), self.pick(LAST)
            return {
                "firstName": f,
                "lastName": la,
                "role": self.maybe(0.8, lambda: self.pick(ROLE)),
                "email": self.maybe(0.8, lambda: f"{f[0]}.{la}@{domain}".lower()),
                "phone": self.maybe(0.4, lambda: f"+33 1 40 00 {self.between(10, 99)} {self.between(10, 99)}"),
            }

        def item() -> dict[str, Any]:
            sku, label, unit_price = self.pick(PRODUCT)
            return {"sku": sku, "label": label, "quantity": self.between(1, 30), "unitPrice": unit_price}

        def order(no: int) -> dict[str, Any]:
            return {
                "orderId": f"O-{n}-{no}",
                "date": self.maybe(0.9, lambda: self.date(START, END)),
                "status": self.maybe(0.9, lambda: self.pick(ORDER_STATUS)),
                "items": [item() for _ in range(self.between(1, 6))],
            }

        created = self.date(START, END)
        doc: dict[str, Any] = {"_id": f"C-S{n:05d}", "type": "company" if company else "individual"}
        doc["name"] = name
        doc["email"] = email
        doc["phone"] = self.maybe(0.7, lambda: "+33 6 " + " ".join(str(self.between(10, 99)) for _ in range(4)))
        doc["status"] = self.pick(["prospect", "active", "active", "inactive"])
        doc["tags"] = self.some(TAG, 3)
        doc["addresses"] = [address(k) for k in ["billing", "shipping"][: self.between(0, 2)]]
        doc["contacts"] = [contact() for _ in range(self.between(0, 3) if company else 0)]
        doc["orders"] = [order(no) for no in range(1, self.between(0, 8) + 1)]
        doc["preferences"] = self.maybe(
            0.6,
            lambda: {
                "language": self.pick(["fr", "en", "de"]),
                "newsletter": self.rand() < 0.5,
                "channels": self.some(["email", "sms", "phone"], 2),
            },
        )
        doc["createdAt"] = created
        doc["updatedAt"] = when(ms_of(created) + self.between(0, 90) * DAY)
        return doc

    # The anomalies: each one, when it needs an element (an order, an item...) and the customer has
    # none, adds one, and takes the field as an earlier one of the same customer may have left it.

    def an_order(self, c: dict[str, Any]) -> dict[str, Any]:
        if c["orders"]:
            return self.pick(c["orders"])
        c["orders"].append({"orderId": f"O-{c['_id']}-x",
                            "items": [{"sku": "CHA-100", "label": "Chaise de bureau", "quantity": 1, "unitPrice": 89.9}]})  # fmt: skip
        return c["orders"][0]

    def an_item(self, c: dict[str, Any]) -> dict[str, Any]:
        return self.pick(self.an_order(c)["items"])

    def an_address(self, c: dict[str, Any]) -> dict[str, Any]:
        if isinstance(c["addresses"], dict):
            return c["addresses"]
        if not c["addresses"]:
            c["addresses"].append({"kind": "billing", "street": "1 rue de la Paix", "city": "Paris",
                                   "postalCode": "75002", "country": "FR"})  # fmt: skip
        return self.pick(c["addresses"])

    @staticmethod
    def tags(c: dict[str, Any]) -> list[Any]:
        return c["tags"] if isinstance(c["tags"], list) else [c["tags"]]

    def make_strange(self, kind: str, c: dict[str, Any]) -> None:
        match kind:
            case "phone-number":
                c["phone"] = float(33600000000 + self.between(0, 99999999))  # a double, as the Node driver writes it
            case "quantity-string":
                i = self.an_item(c)
                i["quantity"] = str(i["quantity"] if is_number(i["quantity"]) else 1)
            # The item first, then the value: the order in which seed.mjs draws them.
            case "quantity-long":
                i = self.an_item(c)
                i["quantity"] = Int64(2**40 + self.between(0, 1000))
            case "quantity-negative":
                i = self.an_item(c)
                i["quantity"] = -self.between(1, 5)
            case "price-string":
                i = self.an_item(c)
                i["unitPrice"] = f"{i['unitPrice'] if is_number(i['unitPrice']) else 9.9:.2f}".replace(".", ",")
            case "price-decimal128":
                i = self.an_item(c)
                i["unitPrice"] = Decimal128(f"{i['unitPrice'] if is_number(i['unitPrice']) else 9.9:.2f}")
            case "date-string":
                o = self.an_order(c)
                d = date if isinstance(date := o.get("date"), datetime) else when(START)
                o["date"] = self.pick([d.strftime("%Y-%m-%d"), d.strftime("%d/%m/%Y")])  # ISO, then fr-FR
            case "date-epoch":
                c["createdAt"] = float(ms_of(c["createdAt"]) if isinstance(c["createdAt"], datetime) else START)
            case "null-fields":
                c["phone"] = None
                c["preferences"] = None
            case "empty-strings":
                c["name"] = ""
                c["email"] = ""
            case "missing-required":
                c.pop(self.pick(["email", "type", "name"]), None)
            case "enum-case":
                c["status"] = str(c["status"]).upper()
                if c.get("type"):
                    c["type"] = c["type"][0].upper() + c["type"][1:]
            case "unknown-fields":
                c["_class"] = "com.example.legacy.Customer"
                c["legacyId"] = self.between(1, 99999)
                c["importedAt"] = when(END)
            case "long-text":
                c["notes"] = "Lorem ipsum dolor sit amet, " * self.between(200, 800)
            case "unicode":
                name = c.get("name")
                c["name"] = f"{'' if name is None else name} 🏢 \u200b שלום 漢字"
            case "object-not-array":
                c["addresses"] = self.an_address(c)
            case "scalar-not-array":
                c["tags"] = ",".join(t for t in self.tags(c) if isinstance(t, str)) or "vip"
            case "mixed-array":
                c["tags"] = [*self.tags(c), self.between(1, 9), None, {"label": "vip"}]
            case "nested-deeper":
                c["preferences"] = {"language": "fr", "newsletter": True,
                                    "channels": [{"type": "email", "optIn": True, "since": when(START)}]}  # fmt: skip
            case "empty-object":
                c["preferences"] = {}
            case "bool-string":
                prefs = c.get("preferences")
                base = {"language": "fr", "channels": []} if prefs is None else prefs
                c["preferences"] = {**base, "newsletter": self.pick(["yes", "no", "true", "1"])}
            case "id-objectid":
                c["_id"] = ObjectId()
            case "duplicate-order-id":
                o = self.an_order(c)
                c["orders"].append({**o, "items": [dict(i) for i in o["items"]]})
            case "odd-field-names":
                a = self.an_address(c)
                a["Code postal"] = a.get("postalCode")
                a.pop("postalCode", None)
                a["pays.iso"] = a.get("country")

    def next(self, n: int) -> dict[str, Any]:
        self.rand = self.normal
        c = clean(self.customer(n))
        self.rand = self.strange
        if self.rand() * 100 < self.strange_pct:
            self.strange_docs += 1
            for k in dict.fromkeys([self.pick(self.kinds) for _ in range(self.between(1, 3))]):
                self.make_strange(k, c)
                self.seen[k] = self.seen.get(k, 0) + 1
        return c


STRANGE = [
    "phone-number", "quantity-string", "quantity-long", "quantity-negative", "price-string", "price-decimal128",
    "date-string", "date-epoch", "null-fields", "empty-strings", "missing-required", "enum-case", "unknown-fields",
    "long-text", "unicode", "object-not-array", "scalar-not-array", "mixed-array", "nested-deeper", "empty-object",
    "bool-string", "id-objectid", "duplicate-order-id", "odd-field-names",
]  # fmt: skip


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mongo-url", default="mongodb://127.0.0.1:27017/?directConnection=true")
    ap.add_argument("--db", default="hello")
    ap.add_argument("--customers", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--strange", type=float, default=5, help="percent of strange customers, 0 to 100")
    ap.add_argument(
        "--strange-kinds",
        default="all",
        help=f"all, none (or empty), or a comma-separated list of: {', '.join(STRANGE)}",
    )
    args = ap.parse_args(argv)
    if args.customers < 0 or not 0 <= args.strange <= 100:
        ap.error("--customers must be 0 or more, --strange 0 to 100")
    kinds = STRANGE if args.strange_kinds == "all" else [k.strip() for k in args.strange_kinds.split(",") if k.strip()]
    kinds = [] if kinds == ["none"] else kinds
    if unknown := [k for k in kinds if k not in STRANGE]:
        ap.error(f"--strange-kinds: unknown {', '.join(unknown)}")
    if not kinds:
        args.strange = 0  # no kind, no strange customer; the normal data is the same whatever the percent

    gen = Customers(args.seed, args.strange, kinds)
    with MongoClient(args.mongo_url) as client:
        client.drop_database(args.db)  # from scratch: whatever an earlier seed or run left there goes
        customers = client[args.db]["customers"]
        for n in range(1, args.customers + 1, 1000):
            customers.insert_many([gen.next(n + i) for i in range(min(1000, args.customers - n + 1))], ordered=True)
        print(f"[seed] {customers.count_documents({})} customers in {args.db}.customers, "
              f"{gen.strange_docs} strange ({args.strange:g}%)")  # fmt: skip
        # Every kind asked for, drawn or not: with few strange customers, some kinds may never come up.
        for k in sorted(set(kinds) | set(gen.seen)):
            print(f"[seed]   {k}: {gen.seen.get(k, 0)}")
        if missing := [k for k in kinds if not gen.seen.get(k)]:
            print(f"[seed] warning: no customer drew {', '.join(missing)}: {gen.strange_docs} strange customers "
                  "are too few for every kind; raise --strange")  # fmt: skip


if __name__ == "__main__":
    main()
