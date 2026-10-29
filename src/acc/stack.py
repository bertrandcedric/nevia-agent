"""The app's stack on Nevia: its computer, and the database its application.properties names (database.kind:
mongo or postgres), MongoDB on a computer of its own that the app reaches through chisel, or a Nevia Postgres.

A sandbox is the same for one run: `<app>-<run>` restored from a checkpoint of the app, on `<mongo>-<run>`, a
branch of the MongoDB computer (a fork of its data), or on `<postgres>-<run>`, a new Postgres.
"""

import secrets
import shlex
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from acc.computer import Computer, secret
from acc.config import Settings
from core.nevia import Nevia
from core.shell import console

Kind = Literal["mongo", "postgres"]
CHISEL_PORT = 8080  # the chisel server's, on the MongoDB computer
CA_FILE = "nevia-pg-ca.pem"  # in the app's sources directory: PG_CA_FILE is relative to it
MONGO_ENV = ("MONGODB_URI", "MONGO_TUNNEL_URL", "CHISEL_USER", "CHISEL_PASSWORD")
POSTGRES_ENV = ("DATABASE_URL", "PG_CA_FILE")
CHECKS = ("/healthz", "/")


def sandbox_name(base: str, run: str) -> str:
    return f"{base}-{run}"


@dataclass
class Stack:
    settings: Settings
    nevia: Nevia
    poll: float = 10.0

    def _on(self, name: str) -> Computer:
        return Computer(name, self.nevia, self.poll)

    # ------------------------------------------------------------------ secrets

    def passwords(self) -> None:
        """MongoDB's root password and chisel's, random, straight into workspace secrets; kept once made."""
        have = {s["name"] for s in self.nevia.secrets.list()}
        s = self.settings
        for name in (s.secret(s.mongo.password_secret), s.secret(s.chisel.password_secret)):
            if name not in have:
                self.nevia.secrets.set(name, secrets.token_hex(24))
                console.log(f"secret {name} made")

    def database_url(self, database: str, secret_name: str) -> str:
        """The database's URI into `secret_name`; returns its CA certificate (not a secret)."""
        conn = self.nevia.databases.connection(database)
        if not conn["uri"]:
            raise RuntimeError(f"{database} has no connection URI yet")
        self.nevia.secrets.set(secret_name, conn["uri"])
        return conn["server_ca"] or ""

    # ------------------------------------------------------------------ MongoDB

    def mongo_up(self, name: str, *, init: bytes | None = None, branch_of: str | None = None) -> None:
        """The MongoDB computer `name` (a branch of `branch_of`, a fork of its data, when given), started, with
        the seed data in `init` (a .tar.gz of local/mongo-init) loaded into an empty database."""
        m, c = self.settings.mongo, self.settings.chisel
        if not self.nevia.computers.exists(name):
            if branch_of:
                console.log(f"branching {branch_of} into {name}")
                self.nevia.computers.branch(branch_of, name)
            else:
                console.log(f"creating computer {name}")
                env = {
                    "MONGO_ROOT_USERNAME": m.user,
                    "MONGO_ROOT_PASSWORD": secret(self.settings.secret(m.password_secret)),
                    "CHISEL_USER": c.user,
                    "CHISEL_PASSWORD": secret(self.settings.secret(c.password_secret)),
                }
                self.nevia.computers.create(name, ports=[{"published": 443, "target": CHISEL_PORT}], env=env)
        pc = self._on(name)
        pc.awake()
        if not any(p["published"] == 443 for p in self.nevia.computers.ports(name)):
            self.nevia.computers.publish(name, 443, CHISEL_PORT)
        image = self.settings.mongodb.image
        pc.put_mongo({"MONGO_IMAGE": image, "MONGO_CACHE_GB": str(self.settings.mongodb.cache_gb), "MONGO_POD": "acc",
                      "MONGO_CONTAINER": "acc-mongo", "MONGO_DB": m.database, "MONGO_PUBLISH": "1",
                      "CHISEL_VERSION": c.version})  # fmt: skip
        if init is not None:
            unpack(pc, init, "mongo-init")
        pc.mongo_up(image)

    def mongo_env(self, mongo: str) -> dict[str, str | None]:
        """What the app needs to reach the MongoDB computer `mongo` through chisel."""
        m, c = self.settings.mongo, self.settings.chisel
        uri = (
            f"mongodb://{m.user}:{secret(self.settings.secret(m.password_secret))}@127.0.0.1:27017/{m.database}"
            "?authSource=admin&directConnection=true"
        )
        env: dict[str, str | None] = {
            "MONGO_TUNNEL_URL": self._on(mongo).url,
            "CHISEL_USER": c.user,
            "CHISEL_PASSWORD": secret(self.settings.secret(c.password_secret)),
            "MONGODB_URI": uri,
        }
        return env | dict.fromkeys(POSTGRES_ENV)

    # ------------------------------------------------------------------ Postgres

    def postgres_up(self, name: str, secret_name: str, *, branch_of: str | None = None) -> tuple[bool, str]:
        """The Postgres database `name` (a branch of `branch_of` when it exists), its URI in `secret_name`.
        Returns whether it was created empty here (a new one wants the seed data), and its CA certificate."""
        created = False
        if not self.nevia.databases.exists(name):
            if branch_of and self.nevia.databases.exists(branch_of):
                console.log(f"branching {branch_of} into {name}")
                self.nevia.databases.branch(branch_of, name)
            else:
                console.log(f"creating Postgres database {name}")
                self.nevia.databases.create(name)
                created = True
        return created, self.database_url(name, secret_name)

    @staticmethod
    def postgres_env(secret_name: str) -> dict[str, str | None]:
        return {"DATABASE_URL": secret(secret_name), "PG_CA_FILE": CA_FILE} | dict.fromkeys(MONGO_ENV)

    # ------------------------------------------------------------------ the app

    def app_up(self, name: str, kind: Kind, env: dict[str, str | None], files: bytes) -> str:
        """The app computer `name` with `env` (None drops a variable), `files` (a .tar.gz of pom.xml and src/,
        or of app.jar) as its sources, built if need be and started; returns its URL once it answers."""
        a, c = self.settings.app, self.settings.chisel
        if not self.nevia.computers.exists(name):
            console.log(f"creating computer {name}")
            stored = {k: v for k, v in env.items() if v is not None}
            self.nevia.computers.create(name, ports=[{"published": 443, "target": a.port}], env=stored)
        else:
            self._on(name).awake()
            self.nevia.computers.update(name, env=env)
        pc = self._on(name)
        pc.put_lib()
        pc.put("app-up.sh", ".nevia-agent/app-up.sh")
        unpack(pc, files, "app/src")
        args = " ".join(map(shlex.quote, [kind, str(a.port), str(a.jdk), a.maven, c.version]))
        pc.run_job("app-up", f"~/.nevia-agent/app-up.sh {args}")
        url = pc.url
        check(url)
        return url

    def wait_dns(self, name: str, host: str, tries: int = 120) -> None:
        """Right after a restore, DNS may not answer yet: wait for 5 lookups of `host` in a row to succeed."""
        ok = 0
        for _ in range(tries):
            out = self._on(name).sh(f"getent hosts {shlex.quote(host)} >/dev/null && echo ok || true")
            ok = ok + 1 if out.strip() == "ok" else 0
            if ok >= 5:
                return
            time.sleep(min(self.poll, 2))
        raise TimeoutError(f"{name} has no DNS for {host}")


def unpack(pc: Computer, archive: bytes, dest: str) -> None:
    """Replace `dest` (under the home) with the contents of a .tar.gz."""
    tmp = ".nevia-agent/upload.tgz"
    pc.upload(archive, tmp)
    q = shlex.quote(dest)
    pc.sh(f"cd && rm -rf {q} && mkdir -p {q} && tar -xmzf {tmp} -C {q} && rm -f {tmp}")


def check(url: str, *, tries: int = 10, delay: float = 3.0) -> None:
    """GET /healthz and / answer with a success."""
    if not url:
        raise RuntimeError("the app has no URL")
    for path in CHECKS:
        for attempt in range(1, tries + 1):
            try:
                httpx.get(url.rstrip("/") + path, timeout=20, follow_redirects=True).raise_for_status()
                break
            except httpx.HTTPError as err:
                if attempt == tries:
                    raise RuntimeError(f"{url}{path} failed: {err}") from None
                time.sleep(delay)
        console.log(f"{url}{path}: ok")
