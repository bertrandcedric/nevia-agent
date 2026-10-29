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

from nevia_kit import Nevia

from core.computer import Computer, agent_command, bundled, secret
from core.config import Runtime
from core.doomed import Doomed
from core.local import runtime
from core.shell import console

KEY_LIFETIME = 7 * 24 * 3600  # seconds; an agent's Nevia key expires a week after it is made


def local_tz() -> dict[str, str]:
    """TZ for the agent's commands: your machine's time zone, so that their logs read as your clock does."""
    if tz := os.environ.get("TZ"):
        return {"TZ": tz}
    zone = str(Path("/etc/localtime").resolve()).partition("zoneinfo/")[2]
    return {"TZ": zone} if zone else {}


@dataclass(frozen=True)
class Wanted:
    """A secret from `.env`: the env var `var`, into the workspace secret `name`."""

    var: str
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

    @property
    def nevia(self) -> Nevia:
        return self.computer.nevia

    @property
    def name(self) -> str:
        return self.computer.name

    @property
    def description(self) -> str:
        return f"{self.agent} agent"  # with its name, how `destroy` finds the principal

    # ------------------------------------------------------------------ setup

    def up(self, env: dict[str, str]) -> None:
        """The agent computer with its key and this code, and the secrets from `env`. Safe to repeat: it makes
        what is missing, and sends the code again."""
        self.secrets(env)
        if not self.nevia.computers.exists(self.name):
            console.log(f"creating computer {self.name}")
            self.nevia.computers.create(self.name)
        self.computer.awake()
        self.key()
        self.install_key()
        self.ship()

    def secrets(self, env: dict[str, str]) -> None:
        have = {s["name"] for s in self.nevia.secrets.list()}
        for w in self.wanted:
            name = self.name_secret(w.name)
            if value := env.get(w.var):
                self.nevia.secrets.set(name, value)
                console.log(f"secret {name} set from {w.var}")
            elif w.always or name not in have:
                raise RuntimeError(f"{w.var} is not set (in .env); setup stores it as the workspace secret {name}")

    def principals(self) -> list[str]:
        """The ids of the agent's private-key principals: named after its computer, or with its description."""
        return [
            e["principal"]["id"]
            for e in self.nevia.access.list()
            if e["principal"].get("auth") == "private_key"
            and (e["principal"].get("name") == self.name or e.get("description") == self.description)
        ]

    def key(self) -> None:
        """The agent's principal (editor) and its key file in the workspace secret, unless the secret is there. A
        principal left without its secret (deleted by hand, or a setup that stopped between the two) gets a new
        key: its name is taken."""
        name = self.name_secret(self.key_secret)
        if any(s["name"] == name for s in self.nevia.secrets.list()):
            return
        if found := self.principals():
            key_file = self.nevia.access.rotate_key(found[0], expires_in=KEY_LIFETIME)["key_file"]
            console.log(f"principal {self.name}: a new key (a week), in {name}")
        else:
            made = self.nevia.access.add_key_principal(
                "editor", name=self.name, description=self.description, expires_in=KEY_LIFETIME
            )
            key_file = made["key_file"]
            console.log(f"principal {self.name} (editor, a week), its key in {name}")
        self.nevia.secrets.set(name, json.dumps(key_file))

    def install_key(self) -> None:
        """The key file on the computer, mode 600: from the secret through a per-command env var, so it is never
        in the computer's env and never passes through this machine."""
        q = shlex.quote(self.key_path)
        self.computer.sh(
            f'umask 077; cd && mkdir -p "$(dirname {q})" && printf %s "$AGENT_KEY" > {q}',
            env={"AGENT_KEY": secret(self.name_secret(self.key_secret))},
        )

    def ship(self) -> None:
        """This code, nevia-kit and agent.toml on the computer, installed by setup-runtime.sh."""
        nv = self.nevia.computers
        nv.upload(self.name, runtime.bundle(), runtime.BUNDLE)
        nv.upload(self.name, bundled("core", "scripts/setup-runtime.sh"), f"{runtime.HOME}/setup-runtime.sh", "700")
        out = self.computer.run_job("runtime", runtime.setup_command(self.runtime, self.name, self.key_path))
        console.log(f"{self.name}: {out.strip().splitlines()[-1]}")

    # ------------------------------------------------------------------ the agent's own commands

    def remote(self, *args: str) -> str:
        """`agent <agent> remote <args>` on the computer, and its output."""
        self.computer.awake()
        return self.computer.sh(agent_command(self.agent, *args), retry=False, env=local_tz() or None)

    def remote_job(self, job: str, *args: str) -> None:
        """The same, as a job that outlasts a dropped exec session; `computer.wait_job` follows it."""
        self.computer.awake()
        self.computer.start_job(job, agent_command(self.agent, *args), env=local_tz() or None)

    # ------------------------------------------------------------------ destroy

    def doomed(self) -> Doomed:
        """The agent computer, its principal, and the secrets from `.env` and of its key."""
        have = {s["name"] for s in self.nevia.secrets.list()}
        names = [self.name_secret(w.name) for w in self.wanted] + [self.name_secret(self.key_secret)]
        computers = [self.name] if self.nevia.computers.exists(self.name) else []
        return Doomed(computers, secrets=[n for n in names if n in have], principals=self.principals())

    def teardown(self, d: Doomed) -> None:
        for name in d.computers:
            self.nevia.computers.delete(name)
            console.log(f"deleted computer {name}")
        for name in d.secrets:
            self.nevia.secrets.delete(name)
            console.log(f"deleted secret {name}")
        for principal in d.principals:
            self.nevia.access.remove(principal)
            console.log(f"removed principal {principal}")
