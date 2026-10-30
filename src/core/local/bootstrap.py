"""What your machine does on Nevia, and nothing more: make the agent computer, its principal and key, and the
secrets from `.env`; send this code there and run it (`agent <m2p|acc> remote ...`); delete them at the end.
Everything else, the agent does from its computer, signed in with its own key.
"""

import json
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import typer

from core.computer import Computer, agent_command, bundled, secret
from core.config import WORKSPACE_ENV, AgentSettings, Runtime
from core.doomed import Doomed, delete
from core.local import runtime
from core.nevia import Nevia
from core.shell import console, show

KEY_LIFETIME = 7 * 24 * 3600  # seconds; an agent's Nevia key expires a week after it is made


def local_tz() -> dict[str, str]:
    """TZ for the agent's commands: your machine's time zone, so that their logs read as your clock does."""
    if tz := os.environ.get("TZ"):
        return {"TZ": tz}
    zone = str(Path("/etc/localtime").resolve()).partition("zoneinfo/")[2]
    return {"TZ": zone} if zone else {}


def passed_on() -> dict[str, str] | None:
    """The env of the agent's commands on its computer: your time zone (`local_tz`), and the workspace you
    chose instead of agent.toml's (`agent --workspace`), which its own settings then read too."""
    env = local_tz() | ({WORKSPACE_ENV: ws} if (ws := os.environ.get(WORKSPACE_ENV)) else {})
    return env or None


@dataclass(frozen=True)
class Key:
    """A private-key principal your machine makes for the agent (the workspace's admin only may): its key file
    in the workspace secret `secret` (unprefixed), then at `path` on the agent computer, under the home."""

    name: str
    role: str  # editor or viewer
    description: str  # with its name, how `destroy` finds the principal
    secret: str
    path: str


@dataclass(frozen=True)
class Wanted:
    """A secret from `.env`: the env var `name`, into the workspace secret of that name, prefixed."""

    name: str
    always: bool = True  # required at each setup; False: the first time only, kept afterwards


@dataclass
class Bootstrap:
    agent: str  # m2p or acc
    computer: Computer  # the agent computer
    runtime: Runtime
    name_secret: Callable[[str], str]  # a settings' `secret`: the prefixed name
    key_secret: str  # the workspace secret of the key file, unprefixed
    key_path: str  # where the key file goes on the computer, under the home
    wanted: list[Wanted] = field(default_factory=list)
    other_keys: list[Key] = field(default_factory=list)  # besides the agent's own

    @classmethod
    def of(
        cls, s: AgentSettings, computer: Computer, extra_wanted: list[Wanted] | None = None,
        other_keys: list[Key] | None = None,
    ) -> Bootstrap:  # fmt: skip
        """An agent's, from its settings: its computer, its key (and `other_keys`), and the secrets from `.env`
        it wants, `extra_wanted` and the gateway key's (when there is a gateway)."""
        wanted = [*(extra_wanted or []), *([Wanted(s.ai.gateway_secret)] if s.ai.gateway_url else [])]
        return cls(s.AGENT, computer, s.runtime, s.secret, s.agent.secret, s.agent.key_path, wanted, other_keys or [])

    @property
    def nevia(self) -> Nevia:
        return self.computer.nevia

    @property
    def name(self) -> str:
        return self.computer.name

    @property
    def description(self) -> str:
        return f"{self.agent} agent"  # with its name, how `destroy` finds the principal

    @property
    def keys(self) -> list[Key]:
        """The agent's own key (editor), then the others."""
        own = Key(self.name, "editor", self.description, self.key_secret, self.key_path)
        return [own, *self.other_keys]

    # ------------------------------------------------------------------ setup

    def up(self, env: dict[str, str]) -> None:
        """The agent computer with its key and this code, and the secrets from `env`. Safe to repeat: it makes
        what is missing, and sends the code again."""
        if self.nevia.ensure_workspace():
            console.log(f"workspace {self.nevia.workspace} made, you its admin")
        self.secrets(env)
        if not self.nevia.computers.exists(self.name):
            console.log(f"creating computer {self.name}")
            self.nevia.computers.create(self.name)
        self.computer.awake()
        self.key()
        self.install_key()
        self.ship()

    def secrets(self, env: dict[str, str]) -> None:
        have = self.nevia.secrets.names()
        for w in self.wanted:
            name = self.name_secret(w.name)
            if value := env.get(w.name):
                self.nevia.secrets.set(name, value)
                console.log(f"secret {name} set from {w.name}")
            elif w.always or name not in have:
                raise RuntimeError(f"{w.name} is not set (in .env); setup stores it as the workspace secret {name}")

    def principals(self, key: Key | None = None) -> list[str]:
        """The ids of the agent's private-key principals (`key`'s only, when given): each named after its key, or
        with its description."""
        keys = [key] if key else self.keys
        return [
            e["principal"]["id"]
            for e in self.nevia.access.list()
            if e["principal"].get("auth") == "private_key"
            and any(e["principal"].get("name") == k.name or e.get("description") == k.description for k in keys)
        ]

    def key(self) -> None:
        """Each of `keys`: its principal and its key file in its workspace secret, unless the secret is there. A
        principal left without its secret (deleted by hand, or a setup that stopped between the two) gets a new
        key: its name is taken."""
        for k in self.keys:
            name = self.name_secret(k.secret)
            if self.nevia.secrets.exists(name):
                continue
            if found := self.principals(k):
                key_file = self.nevia.access.rotate_key(found[0], expires_in=KEY_LIFETIME)["key_file"]
                console.log(f"principal {k.name}: a new key (a week), in {name}")
            else:
                made = self.nevia.access.add_key_principal(
                    k.role, name=k.name, description=k.description, expires_in=KEY_LIFETIME
                )
                key_file = made["key_file"]
                console.log(f"principal {k.name} ({k.role}, a week), its key in {name}")
            self.nevia.secrets.set(name, json.dumps(key_file))

    def install_key(self) -> None:
        """Each key file on the computer, mode 600: from its secret through a per-command env var, so it is never
        in the computer's env and never passes through this machine."""
        for k in self.keys:
            q = shlex.quote(k.path)
            self.computer.sh(
                f'umask 077; cd && mkdir -p "$(dirname {q})" && printf %s "$AGENT_KEY" > {q}',
                env={"AGENT_KEY": secret(self.name_secret(k.secret))},
            )

    def ship(self) -> None:
        """This code, nevia-kit and agent.toml on the computer, installed by setup-runtime.sh with the nevia CLI,
        signed in with the key file."""
        nv = self.nevia.computers
        nv.upload(self.name, runtime.bundle(), runtime.BUNDLE)
        nv.upload(self.name, bundled("core", "scripts/setup-runtime.sh"), f"{runtime.HOME}/setup-runtime.sh", "700")
        out = self.computer.run_job(
            "runtime", runtime.setup_command(self.runtime, self.nevia.workspace, self.name, self.key_path)
        )
        console.log(f"{self.name}: {out.strip().splitlines()[-1]}")

    # ------------------------------------------------------------------ the agent's own commands

    def remote(self, *args: str) -> str:
        """`agent <agent> remote <args>` on the computer, and its output."""
        self.computer.awake()
        return self.computer.sh(agent_command(self.agent, *args), retry=False, env=passed_on())

    def remote_json(self, *args: str) -> str:
        """The same's last line: the JSON it prints for your machine, after what it may have logged."""
        lines = self.remote(*args).strip().splitlines()
        if not lines:
            raise RuntimeError(f"`agent {self.agent} remote {' '.join(args)}` printed nothing")
        return lines[-1]

    def remote_job(self, job: str, *args: str) -> None:
        """The same, as a job that outlasts a dropped exec session; `computer.wait_job` follows it."""
        self.computer.awake()
        self.computer.start_job(job, agent_command(self.agent, *args), env=passed_on())

    # ------------------------------------------------------------------ destroy

    def doomed(self) -> Doomed:
        """The agent computer, its principals, and the secrets from `.env` and of its keys."""
        have = self.nevia.secrets.names()
        names = [self.name_secret(w.name) for w in self.wanted] + [self.name_secret(k.secret) for k in self.keys]
        computers = [self.name] if self.nevia.computers.exists(self.name) else []
        return Doomed(computers, secrets=[n for n in names if n in have], principals=self.principals())

    def setup(self, env: dict[str, str]) -> None:
        """`up`, then the agent's own setup on its computer (`agent <agent> remote setup`), its log shown as it
        goes."""
        self.up(env)
        job = f"{self.agent}-setup"
        self.remote_job(job, "setup")
        self.computer.follow_job(job, show)

    def destroy(self, *, yes: bool) -> None:
        """Everything the agent made, then the agent computer, its key and secrets: listed first, and asked
        (unless `yes`). The agent deletes its part (`agent <agent> remote destroy --yes`), your machine the rest."""
        self.listed_then_deleted(
            "destroy", yes=yes, mine=self.doomed(),
            question="Delete all of these? It can't be undone (collected runs in build/ stay)",
        )  # fmt: skip

    def listed_then_deleted(  # noqa: PLR0913 - how each command lists and asks
        self, command: str, *args: str, yes: bool, mine: Doomed | None = None, question: str = "Delete all of these?",
        nothing: str = "nothing to delete", files_alone: bool = True,
    ) -> None:  # fmt: skip
        """`clean` and `destroy`: what `agent <agent> remote <command> <args>` would delete (`Doomed`, as JSON) and
        `mine`, what your machine deletes after it, listed, then asked (unless `yes`); then the agent deletes its
        part (the same with `--yes`), and your machine `mine`. Nothing is asked when there is nothing to delete
        (or only files, unless `files_alone`)."""
        mine = mine or Doomed()
        theirs = (
            Doomed.from_json(self.remote_json(command, *args)) if self.nevia.computers.exists(self.name) else Doomed()
        )
        both = theirs + mine
        if not (both if files_alone else both.on_nevia):
            show(nothing)
            return
        show("\n".join(both.lines()))
        if not yes:
            typer.confirm(question, abort=True)
        if theirs:
            show(self.remote(command, *args, "--yes"))
        self.teardown(mine)

    def teardown(self, d: Doomed) -> None:
        delete(self.nevia, d)
