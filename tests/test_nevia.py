import json
import subprocess

import pytest
from nevia_kit.testing import FakeApi

from core import nevia
from core.computer import Computer, bundled
from core.config import Runtime
from core.local import runtime
from core.nevia import CliError, CommandFailed, Nevia
from tests.fake_nevia import FakeCli, on_fake


def _cli(fake: FakeApi) -> tuple[Nevia, FakeCli]:
    nv = on_fake(fake)
    assert isinstance(nv.run, FakeCli)
    return nv, nv.run


def test_a_secret_goes_through_stdin_never_the_command_line() -> None:
    fake = FakeApi(validate=True)
    nv, cli = _cli(fake)
    nv.secrets.set("M2P_PG_URL", "postgres://u:s3cr3t@h/db")
    assert cli.calls == [["secrets", "M2P_PG_URL", "--file", "-", "-y"]]
    assert [s["name"] for s in nv.secrets.list()] == ["M2P_PG_URL"]


def test_exec_passes_the_env_and_the_script_and_returns_stdout() -> None:
    fake = FakeApi(validate=True)
    fake.add("computers", "m2p-agent")
    fake.on_exec("echo hi", "hi\n")
    nv, cli = _cli(fake)
    assert nv.computers.sh("m2p-agent", "echo hi", env={"K": "{{secrets.X}}"}) == "hi\n"
    assert cli.calls[-1] == ["exec", "m2p-agent", "--entrypoint", "", "-e", "K={{secrets.X}}", "-n",
                             "--", "bash", "-lc", "echo hi"]  # fmt: skip
    assert fake.execs[-1][2]["env"] == {"K": "{{secrets.X}}"}


def test_a_failed_command_raises_with_its_output() -> None:
    fake = FakeApi(validate=True)
    fake.add("computers", "c")
    fake.on_exec("false", {"exit_code": 2, "stdout": "", "stderr": "boom"})
    nv, _ = _cli(fake)
    with pytest.raises(CommandFailed, match="boom") as err:
        nv.computers.sh("c", "false")
    assert err.value.exit_code == 2


def test_a_missing_resource_is_none_and_files_round_trip() -> None:
    fake = FakeApi(validate=True)
    fake.add("computers", "c")
    nv, _ = _cli(fake)
    assert not nv.computers.exists("nope") and nv.computers.exists("c")
    assert not nv.databases.exists("nope")
    data = bytes(range(256)) * 5000  # over a slice: several execs
    nv.computers.upload("c", data, "dir/f.bin", "644")
    assert fake.files["c", "dir/f.bin"] == data
    assert nv.computers.download("c", "dir/f.bin") == data


def test_publish_keeps_the_other_ports() -> None:
    fake = FakeApi(validate=True)
    fake.add("computers", "c")
    nv, cli = _cli(fake)
    nv.computers.publish("c", 443, 8080)
    nv.computers.publish("c", 8443, 9000)
    assert cli.calls[-2][:2] == ["set", "c"] and cli.calls[-2][2:] == ["-p", "443:8080", "-p", "8443:9000"]
    assert {(p["published"], p["target"]) for p in nv.computers.ports("c")} == {(443, 8080), (8443, 9000)}


def test_a_key_principal_comes_with_its_key_file() -> None:
    fake = FakeApi(validate=True)
    nv, cli = _cli(fake)
    made = nv.access.add_key_principal("editor", name="m2p-agent", description="m2p agent", expires_in=7 * 86400)
    assert isinstance(made["key_file"], dict)
    assert cli.calls[-1][-4:] == ["--expires-in", "7d", "--out", "-"]
    assert [e["description"] for e in nv.access.list()] == ["m2p agent"]


def test_the_cli_is_retried_while_busy_and_its_errors_read(monkeypatch) -> None:
    monkeypatch.setattr(nevia.time, "sleep", lambda _s: None)
    answers = [6, 6, 0]

    def run(argv: list[str], _stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
        code = answers.pop(0)
        return subprocess.CompletedProcess(argv, code, b'{"computers": []}' if code == 0 else b"", b"")

    assert Nevia("w", run=run).computers.list() == []
    err = json.dumps({"error": {"message": "role does not allow it", "exit_code": 4}}).encode()
    nv = Nevia("w", run=lambda argv, _s: subprocess.CompletedProcess(argv, 4, err, b""))
    with pytest.raises(CliError, match="role does not allow it"):
        nv.databases.list()


def test_the_runtime_installs_the_cli_signed_in_with_the_key_file() -> None:
    cmd = runtime.setup_command(Runtime(uv="1", python="3.15", nevia="0.1.13"), "default", "m2p-agent", "k.json")
    assert cmd.endswith("setup-runtime.sh 1 3.15 0.1.13 default m2p-agent k.json")
    script = bundled("core", "scripts/setup-runtime.sh").decode()
    assert "NEVIA_VERSION=$nevia" in script and 'auth use-key-file "$HOME/$key"' in script
    assert 'workspace use "$workspace"' in script and "PATH=$HOME/.local/bin" in script


def test_the_workspace_is_made_once() -> None:
    kit = FakeApi(validate=True).client()
    nv = Nevia("m2p", run=FakeCli(kit), _kit=kit)
    assert nv.ensure_workspace() is True
    assert nv.ensure_workspace() is False
    assert [w["name"] for w in kit.workspaces.list()].count("m2p") == 1


def test_a_missing_computer_says_in_which_workspace() -> None:
    class Pc(Computer):
        PACKAGE, JOBS_DIR = "m2p", "$HOME/.x"

    with pytest.raises(RuntimeError, match="no computer m2p-agent in workspace default: `setup` makes it"):
        Pc("m2p-agent", on_fake(FakeApi(validate=True)), 0).awake()
