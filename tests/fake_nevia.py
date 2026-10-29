"""A fake `nevia` CLI for `core.nevia.Nevia`: each command line it gets is read as the CLI would read it, then
done on nevia-kit's FakeApi, so that the tests check both the command lines and what they do on the API."""

import json
import subprocess
from pathlib import Path
from typing import Any, cast

from nevia_kit import Nevia as Kit
from nevia_kit.errors import CommandFailed, NotFound
from nevia_kit.testing import FakeApi

from core.nevia import Nevia


def _opts(args: list[str], *flags: str) -> tuple[dict[str, list[str]], list[str]]:
    """The options (repeatable, valued unless in `flags`) and the positional arguments; `--` ends them."""
    opts: dict[str, list[str]] = {}
    pos: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += ["--", *args[i + 1 :]]
            break
        if a.startswith("-") and a not in ("-",):
            if a in flags:
                opts.setdefault(a, []).append("")
                i += 1
                continue
            opts.setdefault(a, []).append(args[i + 1])
            i += 2
            continue
        pos.append(a)
        i += 1
    return opts, pos


FLAGS = ("--wait", "-y", "--yes", "-n", "--uri", "--private-key", "--json")


def _ports(values: list[str]) -> Any:  # nevia-kit's PortMapping list
    out = []
    for v in values:
        if v == "none":
            continue
        mapping, _, proto = v.partition("/")
        published, _, target = mapping.partition(":")
        p: dict[str, Any] = {"published": int(published), "target": int(target or published)}
        if proto == "h2":
            p["protocol"] = "http2"
        out.append(p)
    return out


def _env(values: list[str]) -> dict[str, str]:
    return dict(v.split("=", 1) for v in values)


class FakeCli:
    def __init__(self, kit: Kit) -> None:
        self.kit = kit
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
        args = argv[3:]  # nevia --workspace WS
        self.calls.append(args)
        if args and args[0] == "--json":
            args = args[1:]
        try:
            out = self.dispatch(args, stdin)
        except NotFound as err:
            return self._done(argv, 3, "", json.dumps({"error": {"message": str(err), "exit_code": 3}}))
        except CommandFailed as err:
            return self._done(argv, err.exit_code, err.stdout, err.stderr)
        return self._done(argv, 0, out if isinstance(out, str) else json.dumps(out), "")

    @staticmethod
    def _done(argv: list[str], code: int, out: str, err: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, code, out.encode(), err.encode())

    def dispatch(self, args: list[str], stdin: bytes | None) -> Any:  # noqa: PLR0911, PLR0912, PLR0915 - one branch per command
        kit = self.kit
        cmd, rest = args[0], args[1:]
        if cmd in ("pg", "checkpoint", "secrets", "access") and rest and not rest[0].startswith("-"):
            sub = rest[0]
            if cmd == "secrets" and sub not in ("ls", "unset"):
                sub = ""
            else:
                rest = rest[1:]
        else:
            sub = ""
        if cmd == "access" and sub == "keys":
            sub, rest = f"keys {rest[0]}", rest[1:]
        o, pos = _opts(rest, *FLAGS)
        match cmd, sub:
            case "ls", "":
                return {"computers": kit.computers.list(), "has_more": False}
            case "status", "":
                return {"computer": kit.computers.get(pos[0])}
            case "new", "":
                kit.computers.create(pos[0], ports=_ports(o.get("-p", [])) or None, env=_env(o.get("-e", [])) or None)
            case "set", "":
                env: dict[str, str | None] = {**_env(o.get("-e", [])), **dict.fromkeys(o.get("--unset-env", []))}
                ports = _ports(o["-p"]) if "-p" in o else None
                kit.computers.update(pos[0], env=env or None, ports=ports)
            case "rm", "":
                kit.computers.delete(pos[0])
            case "branch", "":
                kit.computers.branch(o["-s"][0], pos[0])
            case "awake", "":
                kit.computers.wake(pos[0])
            case "restore", "":
                kit.checkpoints.restore(o["-c"][0], pos[0], o["--name"][0])
            case "exec", "":
                argv = pos[pos.index("--") + 1 :]
                text = None if stdin is None else stdin.decode()
                env = _env(o.get("-e", []))
                r = kit.computers.exec(pos[0], argv, env=env or None, stdin=text, check=True)
                return r["stdout"]
            case "checkpoint", "ls":
                return {"checkpoints": kit.checkpoints.list(o["-c"][0])}
            case "checkpoint", "create":
                kit.checkpoints.create(o["-c"][0], o["--name"][0])
            case "checkpoint", "rm":
                kit.checkpoints.delete(o["-c"][0], pos[0])
            case "pg", "ls":
                return {"databases": kit.databases.list()}
            case "pg", "status":
                return {"database": kit.databases.get(pos[0])}
            case "pg", "new":
                kit.databases.create(pos[0])
            case "pg", "rm":
                kit.databases.delete(pos[0])
            case "pg", "branch":
                kit.databases.branch(pos[0], o["-b"][0])
            case "pg", "connect":
                conn = kit.databases.connection(pos[0])
                if "--ca" in o:
                    Path(o["--ca"][0]).write_text(conn["server_ca"] or "")
                return (conn["uri"] or "") + "\n"
            case "secrets", "ls":
                return {"secrets": kit.secrets.list()}
            case "secrets", "unset":
                for name in pos:
                    kit.secrets.delete(name)
            case "secrets", "":
                kit.secrets.set(pos[0], (stdin or b"").decode())
            case "access", "ls":
                return {"access": kit.access.list()}
            case "access", "add":
                made = kit.access.add_key_principal(
                    cast("Any", o["--role"][0]),
                    name=o["--name"][0],
                    description=o["--description"][0],
                    expires_in=604800,
                )
                return json.dumps(made["key_file"])
            case "access", "keys rotate":
                return json.dumps(kit.access.rotate_key(pos[0], expires_in=604800)["key_file"])
            case "access", "rm":
                kit.access.remove(pos[0])
            case _:
                raise AssertionError(f"the fake nevia has no {cmd} {sub}")
        return ""


def on_fake(api: FakeApi) -> Nevia:
    """`core.nevia.Nevia` on the fake CLI over `api`, its tunnels on nevia-kit's client of it."""
    kit = api.client()
    return Nevia(kit.workspace or "default", run=FakeCli(kit), _kit=kit)
