"""The app's stack on Nevia: its computer, and the database its application.properties names (database.kind:
mongo or postgres), MongoDB on a computer of its own that the app reaches through chisel, or a Nevia Postgres.

A run deploys it from its clone (`deploy`), then its sandbox, the same for that run: `<app>-<run>` restored from a
checkpoint of the app (`sandbox`), on `<mongo>-<run>`, a branch of the MongoDB computer (a fork of its data), or
on `<postgres>-<run>`, a new Postgres. `RunNames` makes these names, and the others a run makes, and recognises
them for `clean`.
"""

import io
import re
import secrets
import shlex
import tarfile
import time
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from acc.computer import MONGO_CONTAINER, MONGO_POD, Computer, secret
from acc.config import Settings
from acc.run import Kind
from core.computer import HOME, MONGO_PORT
from core.config import RUN_ID
from core.nevia import Nevia
from core.shell import console

CHISEL_PORT = 8080  # the chisel server's, on the MongoDB computer
HTTPS_PORT = 443  # where Nevia publishes a computer's HTTPS: the app's, and chisel's on the MongoDB computer
CA_FILE = "nevia-pg-ca.pem"  # in the app's sources directory: PG_CA_FILE is relative to it
MONGO_ENV = ("MONGODB_URI", "MONGO_TUNNEL_URL", "CHISEL_USER", "CHISEL_PASSWORD")
POSTGRES_ENV = ("DATABASE_URL", "PG_CA_FILE")
CHECKS = ("/healthz", "/")
CHECK_TRIES, CHECK_DELAY = 10, 3.0  # each of CHECKS, seconds apart
DNS_TRIES = 120  # lookups, until DNS_OK in a row succeed
DNS_OK = 5


RUN_SUFFIX = re.compile(rf"({RUN_ID.pattern})$")  # a run's id ends each name it makes
Made = Literal["sandbox", "mongo_fork", "pg", "pg_secret", "dev_db", "dev_secret", "checkpoint"]


@dataclass(frozen=True)
class RunNames:
    """The Nevia names of what a run makes, from the names in agent.toml and the run's id: what makes them and
    what deletes them (`matches`) both read them here."""

    settings: Settings
    run: str = ""  # none: `matches` takes any run's names

    @property
    def sandbox(self) -> str:
        """The app's computer for the run, restored from `checkpoint`."""
        return f"{self.settings.app.computer}-{self.run}"

    @property
    def mongo_fork(self) -> str:
        """The sandbox's MongoDB computer, a branch of the stack's."""
        return f"{self.settings.mongo.computer}-{self.run}"

    @property
    def pg(self) -> str:
        """The sandbox's Postgres, its URI in `pg_secret`."""
        return f"{self.settings.postgres.name}-{self.run}"

    @property
    def pg_secret(self) -> str:
        return f"{self.settings.secret(self.settings.postgres.secret)}_{self.run}"

    @property
    def dev_db(self) -> str:
        """The AI's own empty Postgres for a migration, its URI in `dev_secret`."""
        return f"{self.settings.app.computer}-db-{self.run}"

    @property
    def dev_secret(self) -> str:
        return f"{self.settings.secret(self.settings.postgres.secret)}_DEV_{self.run}"

    @property
    def checkpoint(self) -> str:
        """Of the app, for the sandbox."""
        return f"{self.settings.prefix}{self.run}"

    def made(self) -> dict[Made, str]:
        return {"sandbox": self.sandbox, "mongo_fork": self.mongo_fork, "pg": self.pg, "pg_secret": self.pg_secret,
                "dev_db": self.dev_db, "dev_secret": self.dev_secret, "checkpoint": self.checkpoint}  # fmt: skip

    def computers(self, names: Iterable[str], *, but: str = "") -> list[str]:
        """Of the computers `names`, the runs' sandboxes, then their MongoDB forks (the order they are deleted in),
        but those of the run `but`."""
        names = list(names)
        kinds: tuple[Made, ...] = ("sandbox", "mongo_fork")
        return [c for k in kinds for c in sorted(x for x in names if self.matches(x, k) not in {None, but})]

    def matches(self, name: str, *made: Made) -> str | None:
        """The run whose name of `made` `name` is (this run only, when it has one), or None."""
        m = RUN_SUFFIX.search(name)
        if not m or self.run not in {"", m[1]}:
            return None
        names = RunNames(self.settings, m[1]).made()
        return m[1] if any(names[k] == name for k in made) else None


@dataclass
class Stack:
    settings: Settings
    nevia: Nevia
    poll: float = 10.0

    def _on(self, name: str) -> Computer:
        return Computer(name, self.nevia, self.poll)

    # ------------------------------------------------------------------ the stack, and a run's sandbox

    def deploy(self, repo: Path, kind: Kind, pg_init: Callable[[str], None], run: str) -> str:
        """The app of the clone in `repo` (on this computer) on its computer, with the database of `kind`, for the
        run `run`. The app and MongoDB computers are deleted first (their data with them), so the stack is never an
        older version, and starts from the seed data: a new Postgres gets it from `pg_init`, given the secret of
        its URI. Returns the app's URL."""
        s, n = self.settings, RunNames(self.settings)
        # The earlier runs' sandboxes before: Nevia refuses to delete the app while a computer restored from one
        # of its checkpoints lives. Their forks of MongoDB with them; their databases and secrets stay (`clean`).
        for name in n.computers((c["name"] for c in self.nevia.computers.list()), but=run):
            console.log(f"deleting computer {name}, left by run {n.matches(name, 'sandbox', 'mongo_fork')}")
            self.nevia.computers.delete(name)
        for name in (s.app.computer, s.mongo.computer):
            if self.nevia.computers.exists(name):
                console.log(f"deleting computer {name}")
                self.nevia.computers.delete(name)
        if kind == "mongo":
            self.mongo_up(s.mongo.computer, init=mongo_init(repo))
            env, ca = self.mongo_env(s.mongo.computer), ""
        else:
            sec = s.secret(s.postgres.secret)
            created, ca = self.postgres_up(s.postgres.name, sec, app=True)
            if created:
                pg_init(sec)
            env = self.postgres_env(sec)
        url = self.app_up(s.app.computer, kind, env, sources(repo, ca))
        console.log(f"{s.app.computer}: {url}")
        return url

    def sandbox(self, run: str) -> str:
        """A checkpoint of the app restored as the run's sandbox, once its DNS answers; its name."""
        app, n = self.settings.app.computer, RunNames(self.settings, run)
        checkpoint, sandbox = n.checkpoint, n.sandbox
        if not any(c["name"] == checkpoint and c["state"] == "ready" for c in self.nevia.checkpoints.list(app)):
            console.log(f"checkpoint {checkpoint} of {app}")
            self.nevia.checkpoints.create(app, checkpoint)
        if not self.nevia.computers.exists(sandbox):
            console.log(f"restoring {checkpoint} as {sandbox}")
            self.nevia.checkpoints.restore(app, checkpoint, sandbox)
        self.wait_dns(sandbox, urllib.parse.urlsplit(Computer(app, self.nevia).url).hostname or "")
        return sandbox

    # ------------------------------------------------------------------ secrets

    def passwords(self) -> None:
        """MongoDB's root password and chisel's, random, straight into workspace secrets; kept once made."""
        have = self.nevia.secrets.names()
        s = self.settings
        for name in (s.secret(s.mongo.password_secret), s.secret(s.chisel.password_secret)):
            if name not in have:
                self.nevia.secrets.set(name, secrets.token_hex(24))
                console.log(f"secret {name} made")

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
                self.nevia.computers.create(name, ports=[{"published": HTTPS_PORT, "target": CHISEL_PORT}], env=env)
        pc = self._on(name)
        pc.awake()
        if not any(p["published"] == HTTPS_PORT for p in self.nevia.computers.ports(name)):
            self.nevia.computers.publish(name, HTTPS_PORT, CHISEL_PORT)
        pc.put_mongo(
            self.settings.mongodb,
            pod=MONGO_POD,
            container=MONGO_CONTAINER,
            MONGO_DB=m.database,
            MONGO_PUBLISH="1",
            CHISEL_VERSION=c.version,
        )
        if init is not None:
            unpack(pc, init, "mongo-init")
        pc.mongo_up(self.settings.mongodb.image)

    def mongo_uri(self, port: int, database: str = "") -> str:
        """The URI of the stack's MongoDB at 127.0.0.1:`port` (a chisel or a link), its password as the template
        of its secret."""
        m = self.settings.mongo
        return (f"mongodb://{m.user}:{secret(self.settings.secret(m.password_secret))}@127.0.0.1:{port}/{database}"
                "?authSource=admin&directConnection=true")  # fmt: skip

    def mongo_env(self, mongo: str) -> dict[str, str | None]:
        """What the app needs to reach the MongoDB computer `mongo` through chisel."""
        c = self.settings.chisel
        env: dict[str, str | None] = {
            "MONGO_TUNNEL_URL": self._on(mongo).url,
            "CHISEL_USER": c.user,
            "CHISEL_PASSWORD": secret(self.settings.secret(c.password_secret)),
            "MONGODB_URI": self.mongo_uri(MONGO_PORT, self.settings.mongo.database),
        }
        return env | dict.fromkeys(POSTGRES_ENV)

    # ------------------------------------------------------------------ Postgres

    def postgres_up(
        self, name: str, secret_name: str, *, branch_of: str | None = None, app: bool = False
    ) -> tuple[bool, str]:
        """The Postgres database `name` (a branch of `branch_of` when it exists), its URI in `secret_name`: for the
        `app`, its pooler's with `[acc.postgres] pooler`. Returns whether it was created empty here (a new one
        wants the seed data), and its CA certificate."""
        created = False
        if not self.nevia.databases.exists(name):
            if branch_of and self.nevia.databases.exists(branch_of):
                console.log(f"branching {branch_of} into {name}")
                self.nevia.databases.branch(branch_of, name)
            else:
                console.log(f"creating Postgres database {name}")
                self.nevia.databases.create(name)
                created = True
        pg = self.settings.postgres
        if app and pg.pooler:
            return created, self.nevia.databases.store_uri(name, secret_name, pooled=True, params=pg.pooled_params)
        return created, self.nevia.databases.store_uri(name, secret_name)

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
            self.nevia.computers.create(name, ports=[{"published": HTTPS_PORT, "target": a.port}], env=stored)
        else:
            self._on(name).awake()
            self.nevia.computers.update(name, env=env)
        pc = self._on(name)
        pc.put_lib()
        pc.put("app-up.sh", f"{HOME}/app-up.sh")
        unpack(pc, files, "app/src")
        args = " ".join(map(shlex.quote, [kind, str(a.port), str(a.jdk), a.maven, c.version]))
        pc.run_job("app-up", f"~/{HOME}/app-up.sh {args}")
        url = pc.url
        check(url)
        return url

    def wait_dns(self, name: str, host: str) -> None:
        """Right after a restore, DNS may not answer yet: wait for DNS_OK lookups of `host` in a row to succeed.
        The waiting is one command on the computer, a lookup a second: each exec costs a sign-in, at most 30 a
        minute from this address (`core.nevia.THROTTLED`)."""
        out = self._on(name).sh(
            f"ok=0; for _ in $(seq {DNS_TRIES}); do "
            f"if getent hosts {shlex.quote(host)} >/dev/null; then ok=$((ok + 1)); else ok=0; fi; "
            f'[ "$ok" -ge {DNS_OK} ] && {{ echo ok; exit 0; }}; sleep 1; done; echo no',
            retry=True,  # only lookups: safe to send again after a dropped session
        )
        if out.strip().splitlines()[-1:] != ["ok"]:
            raise TimeoutError(f"{name} has no DNS for {host}")


def pack(paths: dict[str, bytes | Path]) -> bytes:
    """A .tar.gz, in memory, of these files (bytes) and of these files or directories on this computer (Path)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in paths.items():
            if isinstance(data, Path):
                tar.add(data, name)
                continue
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def sources(repo: Path, ca: str = "") -> bytes:
    """pom.xml and src/ of the clone in `repo`, as a .tar.gz, with the CA if given."""
    return pack({"pom.xml": repo / "pom.xml", "src": repo / "src", **({CA_FILE: ca.encode()} if ca else {})})


def mongo_init(repo: Path) -> bytes | None:
    """local/mongo-init/*.js of the clone, as a .tar.gz, or nothing when it has none."""
    scripts = sorted((repo / "local/mongo-init").glob("*.js"))
    return pack({f.name: f for f in scripts}) if scripts else None


def unpack(pc: Computer, archive: bytes, dest: str) -> None:
    """Replace `dest` (under the home) with the contents of a .tar.gz."""
    tmp = f"{HOME}/upload.tgz"
    pc.upload(archive, tmp)
    q = shlex.quote(dest)
    pc.sh(f"cd && rm -rf {q} && mkdir -p {q} && tar -xmzf {tmp} -C {q} && rm -f {tmp}")


def check(url: str) -> None:
    """GET /healthz and / answer with a success."""
    if not url:
        raise RuntimeError("the app has no URL")
    for path in CHECKS:
        for attempt in range(1, CHECK_TRIES + 1):
            try:
                httpx.get(url.rstrip("/") + path, timeout=20, follow_redirects=True).raise_for_status()
                break
            except httpx.HTTPError as err:
                if attempt == CHECK_TRIES:
                    raise RuntimeError(f"{url}{path} failed: {err}") from None
                time.sleep(CHECK_DELAY)
        console.log(f"{url}{path}: ok")
