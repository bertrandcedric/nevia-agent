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


FLAGS = ("--wait", "-y", "--yes", "-n", "--uri", "--pooled", "--private-key", "--json")
CONFLICT = 5  # the CLI's exit code for a request the resource's state refuses


class Refused(Exception):
    """What the real API refuses and FakeApi doesn't: the CLI's conflict."""


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
        self.resets: dict[str, int] = {}  # database: its password resets, which FakeApi doesn't keep

    def __call__(self, argv: list[str], stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
        args = argv[3:] if argv[1] == "--workspace" else argv[1:]  # nevia [--workspace WS] ...
        self.calls.append(args)
        if args and args[0] == "--json":
            args = args[1:]
        try:
            out = self.dispatch(args, stdin)
        except NotFound as err:
            return self._done(argv, 3, "", json.dumps({"error": {"message": str(err), "exit_code": 3}}))
        except CommandFailed as err:
            return self._done(argv, err.exit_code, err.stdout, err.stderr)
        except Refused as err:
            return self._done(argv, CONFLICT, "", json.dumps({"error": {"message": str(err), "exit_code": CONFLICT}}))
        return self._done(argv, 0, out if isinstance(out, str) else json.dumps(out), "")

    def _refuse_in_use(self, computer: str) -> None:
        """Nevia refuses to delete a computer while a live computer restored from one of its checkpoints exists;
        FakeApi doesn't."""
        mine = {c["id"] for c in self.kit.checkpoints.list(self.kit.computers.get(computer)["id"])}
        users = [c for c in self.kit.computers.list() if c.get("from_checkpoint") in mine]
        if users:
            n = len(users)
            raise Refused(f"Checkpoint of {computer} is in use by {n} live computer created or restored from it")

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
        # pg branch's --protected is a flag; pg set's takes a value.
        o, pos = _opts(rest, *FLAGS, *(["--protected"] if (cmd, sub) == ("pg", "branch") else []))
        if cmd == "workspace":
            sub, pos = rest[0], rest[1:]
        match cmd, sub:
            case "workspace", "get":
                return {"workspace": kit.workspaces.get(pos[0])}
            case "workspace", "create":
                kit.workspaces.create(pos[0])
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
                self._refuse_in_use(pos[0])
                kit.computers.delete(pos[0])
            case "branch", "":
                kit.computers.branch(o["-s"][0], pos[0])
            case "awake", "":
                kit.computers.wake(pos[0])
            case "suspend", "":
                kit.computers.sleep(pos[0])
            case "ops", "":
                filters = dict(f.split("=", 1) for f in o.get("-f", []))
                return {"operations": kit.operations.list(**filters), "has_more": False, "next_cursor": None}
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
                if kit.databases.get(pos[0]).get("protected"):
                    raise Refused(f"database {pos[0]} is protected: nevia pg set --protected false first")
                kit.databases.delete(pos[0])
            case "pg", "branch":
                kit.databases.branch(pos[0], o["-b"][0], protected=True if "--protected" in o else None)
            case "pg", "set":
                kit.databases.update(pos[0], protected={"true": True, "false": False}[o["--protected"][0]])
            case "pg", "reset-password":
                kit.databases.reset_credentials(pos[0])
                self.resets[pos[0]] = self.resets.get(pos[0], 0) + 1
            case "pg", "connect":
                conn = kit.databases.connection(pos[0])
                if "--ca" in o:
                    Path(o["--ca"][0]).write_text(conn["server_ca"] or "")
                uri = conn["uri"] or ""
                if "--pooled" in o:  # the pooler's, on 6432 of the same host (FakeApi's is the spec's example)
                    uri = uri.replace(":5432/", ":6432/")
                if n := self.resets.get(pos[0]):  # the password as of its latest reset
                    uri = uri.replace(":secret@", f":secret{n}@")
                return uri + "\n"
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


def job_ends(fake: FakeApi, output: str = "", code: str = "0") -> None:
    """Every job ends at once with `code`, `output` its log."""
    fake.on_exec(".exit 2>/dev/null; echo", f"{code}\n{output.splitlines()[-1] if output else ''}\n")
    fake.on_exec("tail -n 300", output)


def exec_env(fake: FakeApi, needle: str) -> dict[str, str]:
    """The env of the first exec whose command line has `needle`."""
    return next(body.get("env") or {} for _, argv, body in fake.execs if needle in " ".join(argv))
