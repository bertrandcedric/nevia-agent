"""Nevia through its CLI, `nevia`, on your machine (its `nevia login`) and on an agent computer (its key file,
NEVIA_KEY_FILE): the computers, databases, checkpoints, secrets and principals both agents use, as `--json` reads
them (the API's documents). Only the tunnels go through nevia-kit (`kit`): the CLI has none.

Files go through `exec` too, base64 over stdin in slices under its 1 MiB, the way nevia-kit sends them.
"""

import base64
import builtins
import hashlib
import json
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import nevia_kit
from nevia_kit import NeviaError

__all__ = ["CliError", "CommandFailed", "Nevia", "NeviaError"]

Runner = Callable[[list[str], bytes | None], subprocess.CompletedProcess[bytes]]
NOT_FOUND, RETRYABLE = 3, 6  # the CLI's exit codes (`nevia --help`)
CHUNK = 512 * 1024  # bytes per exec for a file, base64 under the CLI's 1 MiB of stdin or output
RETRIES = 3


def run_cli(argv: list[str], stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, input=stdin, capture_output=True, check=False)


class CliError(NeviaError):
    """`nevia` failed: its exit code (3 not found, 5 conflict, 6 retryable...) and its message."""

    def __init__(self, args: list[str], code: int, message: str) -> None:
        self.code = code
        super().__init__(f"nevia {' '.join(args[:2])} exited {code}: {message}")


class CommandFailed(CliError):
    """A command on a computer exited nonzero."""

    def __init__(self, args: list[str], code: int, stdout: str, stderr: str) -> None:
        self.exit_code, self.stdout, self.stderr = code, stdout, stderr
        super().__init__(args, code, stderr.strip()[-2000:] or stdout.strip()[-2000:])


def _message(p: subprocess.CompletedProcess[bytes]) -> str:
    for text in (p.stdout, p.stderr):
        try:
            return str(json.loads(text)["error"]["message"])
        except ValueError, KeyError, TypeError:
            continue
    return (p.stderr or p.stdout).decode(errors="replace").strip()


def _duration(seconds: int) -> str:
    return f"{seconds // 86400}d" if seconds % 86400 == 0 else f"{max(seconds // 60, 15)}m"


@dataclass
class Nevia:
    workspace: str
    run: Runner = run_cli
    command: str = "nevia"
    _kit: nevia_kit.Nevia | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.computers = Computers(self)
        self.checkpoints = Checkpoints(self)
        self.databases = Databases(self)
        self.secrets = Secrets(self)
        self.access = Access(self)

    @property
    def kit(self) -> nevia_kit.Nevia:
        """nevia-kit, for its tunnels only, signed in the same way (`nevia auth access-token`, or the key file)."""
        if self._kit is None:
            self._kit = nevia_kit.Nevia(self.workspace)
        return self._kit

    def cli(self, *args: str, stdin: bytes | None = None, retry: bool = False) -> subprocess.CompletedProcess[bytes]:
        """`nevia ARGS` in the workspace; `retry` repeats it while the CLI says the service was busy."""
        argv = [self.command, "--workspace", self.workspace, *args]
        for attempt in range(1, RETRIES + 1):
            p = self.run(argv, stdin)
            if not (retry and p.returncode == RETRYABLE and attempt < RETRIES):
                return p
            time.sleep(2 * attempt)
        return p

    def ensure_workspace(self) -> bool:
        """The workspace, made if it isn't there (whoever runs this becomes its admin); whether it was made."""
        p = self.run([self.command, "--json", "workspace", "get", self.workspace], None)
        if p.returncode == 0:
            return False
        if p.returncode != NOT_FOUND:
            raise CliError(["workspace", "get"], p.returncode, _message(p))
        p = self.run([self.command, "workspace", "create", self.workspace], None)
        if p.returncode:
            raise CliError(["workspace", "create"], p.returncode, _message(p))
        return True

    def ok(self, *args: str, stdin: bytes | None = None) -> str:
        """`nevia ARGS`, its output; any failure raises."""
        p = self.cli(*args, stdin=stdin)
        if p.returncode:
            raise CliError(list(args), p.returncode, _message(p))
        return p.stdout.decode()

    def json(self, *args: str, missing_ok: bool = False) -> Any:
        """`nevia --json ARGS`, read; None for a resource that isn't there, with `missing_ok`."""
        p = self.cli("--json", *args, retry=True)
        if p.returncode == NOT_FOUND and missing_ok:
            return None
        if p.returncode:
            raise CliError(list(args), p.returncode, _message(p))
        return json.loads(p.stdout or b"null")


@dataclass
class _Group:
    nv: Nevia


def _env_flags(env: dict[str, str] | None) -> builtins.list[str]:
    return [arg for k, v in (env or {}).items() for arg in ("-e", f"{k}={v}")]


def _port_flags(ports: list[dict[str, Any]]) -> builtins.list[str]:
    flags = []
    for p in ports:
        h2 = "/h2" if p.get("protocol") == "http2" else ""
        flags += ["-p", f"{p['published']}:{p['target']}{h2}"]
    return flags or ["-p", "none"]


class Computers(_Group):
    def list(self) -> builtins.list[dict[str, Any]]:
        return self.nv.json("ls")["computers"]

    def find(self, name: str) -> dict[str, Any] | None:
        d = self.nv.json("status", name, missing_ok=True)
        return d.get("computer", d) if isinstance(d, dict) else None

    def get(self, name: str) -> dict[str, Any]:
        if (c := self.find(name)) is None:
            raise CliError(["status", name], NOT_FOUND, f"no computer {name}")
        return c

    def exists(self, name: str) -> bool:
        return self.find(name) is not None

    def create(
        self, name: str, *, ports: builtins.list[dict[str, Any]] | None = None, env: dict[str, str] | None = None
    ) -> None:
        self.nv.ok("new", name, "--wait", *(_port_flags(ports) if ports else []), *_env_flags(env))

    def update(self, name: str, *, env: dict[str, str | None]) -> None:
        """`env` merged into the computer's (None removes a variable)."""
        flags = [a for k, v in env.items() for a in (("--unset-env", k) if v is None else ("-e", f"{k}={v}"))]
        self.nv.ok("set", name, *flags)

    def delete(self, name: str) -> None:
        self.nv.ok("rm", name, "-y", "--wait")

    def branch(self, source: str, name: str) -> None:
        self.nv.ok("branch", name, "-s", source, "--wait")

    def wake(self, name: str) -> None:
        self.nv.ok("awake", name, "--wait")

    def ports(self, name: str) -> builtins.list[dict[str, Any]]:
        return self.get(name).get("ports") or []

    def publish(self, name: str, published: int, target: int) -> str:
        """`target` as HTTPS on `published`, keeping the other mappings (the CLI replaces them as a whole)."""
        others = [p for p in self.ports(name) if p["published"] != published]
        self.nv.ok("set", name, *_port_flags([*others, {"published": published, "target": target}]))
        return next((p.get("url") or "" for p in self.ports(name) if p["published"] == published), "")

    def sh(self, name: str, script: str, *, env: dict[str, str] | None = None, stdin: str | None = None,
           retry: bool = False) -> str:  # fmt: skip
        """A bash script in a login shell on the computer (`env` for it only, `{{secrets.NAME}}` templates
        included): its stdout; a nonzero exit raises CommandFailed. `retry` repeats it while the CLI says the
        service was busy: only for a script safe to run twice."""
        args = ["exec", name, "--entrypoint", "", *_env_flags(env), *([] if stdin is not None else ["-n"]),
                "--", "bash", "-lc", script]  # fmt: skip
        p = self.nv.cli(*args, stdin=None if stdin is None else stdin.encode(), retry=retry)
        if p.returncode:
            raise CommandFailed(
                args, p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
            )
        return p.stdout.decode(errors="replace")

    def upload(self, name: str, data: bytes, path: str, mode: str = "600") -> None:
        """`data` at `path` (under the home), in slices, put together then moved in place, checked with sha256."""
        q = shlex.quote(path)
        self.sh(name, f'umask 077; mkdir -p "$(dirname {q})"; rm -f {q}.part.*', retry=True)
        for i in range(0, max(len(data), 1), CHUNK):
            chunk = base64.b64encode(data[i : i + CHUNK]).decode()
            self.sh(name, f"umask 077; base64 -d > {q}.part.{i // CHUNK:06d}", stdin=chunk, retry=True)
        have = self.sh(
            name,
            f"set -e; umask 077; if ls {q}.part.* > /dev/null 2>&1; then cat {q}.part.* > {q}.tmp; "
            f"rm -f {q}.part.*; fi; if [ -e {q}.tmp ]; then chmod {shlex.quote(mode)} {q}.tmp; mv {q}.tmp {q}; fi; "
            f"sha256sum {q} | cut -d' ' -f1",
            retry=True,
        ).strip()
        if have != hashlib.sha256(data).hexdigest():
            raise NeviaError(f"upload of {path} is corrupt on {name}")

    def download(self, name: str, path: str) -> bytes:
        """A file of the computer (under the home), in base64 slices."""
        q = shlex.quote(path)
        size = int(self.sh(name, f"stat -c %s {q}", retry=True).strip())
        parts = []
        for i in range((size + CHUNK - 1) // CHUNK):
            out = self.sh(name, f"dd if={q} bs={CHUNK} skip={i} count=1 status=none | base64 -w0", retry=True)
            parts.append(base64.b64decode("".join(out.split())))
        data = b"".join(parts)
        if len(data) != size:
            raise NeviaError(f"download of {path} from {name} got {len(data)} of {size} bytes")
        return data


class Checkpoints(_Group):
    def list(self, computer: str) -> builtins.list[dict[str, Any]]:
        return self.nv.json("checkpoint", "ls", "-c", computer)["checkpoints"]

    def create(self, computer: str, name: str) -> None:
        self.nv.ok("checkpoint", "create", "-c", computer, "--name", name, "--wait")

    def delete(self, computer: str, name: str) -> None:
        self.nv.ok("checkpoint", "rm", name, "-c", computer, "-y", "--wait")

    def restore(self, computer: str, checkpoint: str, name: str) -> None:
        self.nv.ok("restore", checkpoint, "-c", computer, "--name", name, "--wait")


class Databases(_Group):
    def list(self) -> builtins.list[dict[str, Any]]:
        return self.nv.json("pg", "ls")["databases"]

    def exists(self, name: str) -> bool:
        return self.nv.json("pg", "status", name, missing_ok=True) is not None

    def create(self, name: str) -> None:
        self.nv.ok("pg", "new", name, "--wait")

    def delete(self, name: str) -> None:
        self.nv.ok("pg", "rm", name, "-y", "--wait")

    def branch(self, source: str, name: str) -> None:
        self.nv.ok("pg", "branch", source, "-b", name, "--wait")

    def connection(self, name: str) -> dict[str, str]:
        """Its URI, password included (it goes to a secret or a file, never a log), and its CA certificate."""
        with tempfile.TemporaryDirectory() as d:
            ca = Path(d) / "ca.pem"
            uri = self.nv.ok("pg", "connect", name, "--uri", "--ca", str(ca)).strip()
            return {"uri": uri, "server_ca": ca.read_text() if ca.is_file() else ""}


class Secrets(_Group):
    def list(self) -> builtins.list[dict[str, Any]]:
        return self.nv.json("secrets", "ls")["secrets"]

    def set(self, name: str, value: str) -> None:
        """The value through stdin, never on a command line."""
        self.nv.ok("secrets", name, "--file", "-", "-y", stdin=value.encode())

    def delete(self, name: str) -> None:
        self.nv.ok("secrets", "unset", name, "-y")


class Access(_Group):
    def list(self) -> builtins.list[dict[str, Any]]:
        return self.nv.json("access", "ls")["access"]

    def add_key_principal(self, role: str, *, name: str, description: str, expires_in: int) -> dict[str, Any]:
        """A private-key principal; its key file (`key_file`), shown once, from the CLI's stdout."""
        out = self.nv.ok("access", "add", "--private-key", "--role", role, "--name", name,
                         "--description", description, "--expires-in", _duration(expires_in), "--out", "-")  # fmt: skip
        return {"key_file": json.loads(out)}

    def rotate_key(self, principal: str, *, expires_in: int) -> dict[str, Any]:
        out = self.nv.ok("access", "keys", "rotate", principal, "--expires-in", _duration(expires_in), "--out", "-")
        return {"key_file": json.loads(out)}

    def remove(self, principal: str) -> None:
        self.nv.ok("access", "rm", principal, "-y")
