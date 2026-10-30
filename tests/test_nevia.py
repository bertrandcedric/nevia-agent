import json
import subprocess
from dataclasses import replace

import pytest
from nevia_kit.testing import FakeApi

from core import nevia as core_nevia
from core.computer import Computer, bundled
from core.config import Runtime
from core.local import runtime
from core.nevia import CliError, CommandFailed, Nevia, run_cli
from tests.fake_nevia import FakeCli


def _cli(nv: Nevia) -> FakeCli:
    assert isinstance(nv.run, FakeCli)
    return nv.run


def test_a_secret_goes_through_stdin_never_the_command_line(fake: FakeApi, nevia: Nevia) -> None:
    cli = _cli(nevia)
    nevia.secrets.set("M2P_PG_URL", "postgres://u:s3cr3t@h/db")
    assert cli.calls == [["secrets", "M2P_PG_URL", "--file", "-", "-y"]]
    assert [s["name"] for s in nevia.secrets.list()] == ["M2P_PG_URL"]


def test_a_database_s_uri_goes_into_a_secret_and_its_ca_comes_back(fake: FakeApi, nevia: Nevia) -> None:
    cli = _cli(nevia)
    nevia.databases.create("m2p-pg")
    ca = nevia.databases.store_uri("m2p-pg", "M2P_PG_URL")
    assert ca == (fake.client().databases.connection("m2p-pg")["server_ca"] or "")
    assert nevia.secrets.names() == {"M2P_PG_URL"} and nevia.secrets.exists("M2P_PG_URL")
    assert not nevia.secrets.exists("M2P_OTHER")
    assert not any("postgres" in " ".join(c) for c in cli.calls)  # the URI only ever on stdin


def test_a_database_s_protection_and_password_go_through_the_cli(fake: FakeApi, nevia: Nevia) -> None:
    cli = _cli(nevia)
    nevia.databases.create("m2p-pg")
    nevia.databases.protect("m2p-pg")
    nevia.databases.branch("m2p-pg", "m2p-pg-copy", protected=True)
    nevia.databases.reset_password("m2p-pg")
    assert cli.calls[1:] == [
        ["pg", "set", "--protected", "true", "m2p-pg"],
        ["pg", "branch", "m2p-pg", "-b", "m2p-pg-copy", "--protected", "--wait"],
        ["pg", "reset-password", "-y", "m2p-pg"],
    ]
    assert fake.get("databases", "m2p-pg")["protected"] and fake.get("databases", "m2p-pg-copy")["protected"]
    assert [c.op for c in fake.calls if c.op == "resetCredentials"] == ["resetCredentials"]
    with pytest.raises(CliError, match="protected"):
        nevia.databases.delete("m2p-pg")  # as Nevia refuses it
    nevia.databases.protect("m2p-pg", False)
    nevia.databases.delete("m2p-pg")
    assert fake.find("databases", "m2p-pg") is None


def test_exec_passes_the_env_and_the_script_and_returns_stdout(fake: FakeApi, nevia: Nevia) -> None:
    fake.add("computers", "m2p-agent")
    fake.on_exec("echo hi", "hi\n")
    cli = _cli(nevia)
    assert nevia.computers.sh("m2p-agent", "echo hi", env={"K": "{{secrets.X}}"}) == "hi\n"
    assert cli.calls[-1] == ["exec", "m2p-agent", "--entrypoint", "", "-e", "K={{secrets.X}}", "-n",
                             "--", "bash", "-lc", "echo hi"]  # fmt: skip
    assert fake.execs[-1][2]["env"] == {"K": "{{secrets.X}}"}


def test_a_failed_command_raises_with_its_output(fake: FakeApi, nevia: Nevia) -> None:
    fake.add("computers", "c")
    fake.on_exec("false", {"exit_code": 2, "stdout": "", "stderr": "boom"})
    with pytest.raises(CommandFailed, match="boom") as err:
        nevia.computers.sh("c", "false")
    assert err.value.exit_code == 2


def test_a_missing_resource_is_none_and_files_round_trip(fake: FakeApi, nevia: Nevia) -> None:
    fake.add("computers", "c")
    assert not nevia.computers.exists("nope") and nevia.computers.exists("c")
    assert not nevia.databases.exists("nope")
    data = bytes(range(256)) * 5000  # over a slice: several execs
    nevia.computers.upload("c", data, "dir/f.bin", "644")
    assert fake.files["c", "dir/f.bin"] == data
    assert nevia.computers.download("c", "dir/f.bin") == data


def test_publish_keeps_the_other_ports(fake: FakeApi, nevia: Nevia) -> None:
    fake.add("computers", "c")
    cli = _cli(nevia)
    nevia.computers.publish("c", 443, 8080)
    nevia.computers.publish("c", 8443, 9000)
    assert cli.calls[-2][:2] == ["set", "c"] and cli.calls[-2][2:] == ["-p", "443:8080", "-p", "8443:9000"]
    assert {(p["published"], p["target"]) for p in nevia.computers.ports("c")} == {(443, 8080), (8443, 9000)}


def test_a_key_principal_comes_with_its_key_file(fake: FakeApi, nevia: Nevia) -> None:
    cli = _cli(nevia)
    made = nevia.access.add_key_principal("editor", name="m2p-agent", description="m2p agent", expires_in=7 * 86400)
    assert isinstance(made["key_file"], dict)
    assert cli.calls[-1][-4:] == ["--expires-in", "7d", "--out", "-"]
    assert [e["description"] for e in nevia.access.list()] == ["m2p agent"]


def test_the_cli_is_retried_while_busy_and_its_errors_read(monkeypatch) -> None:
    monkeypatch.setattr("core.nevia.time.sleep", lambda _s: None)
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
    # The pinned CLI offers no newer release: in the script's own env, and in bin/agent's.
    assert script.count("NEVIA_NO_UPDATE_CHECK=1") == 2


def test_the_cli_s_update_check_is_off_on_an_agent_computer_only(monkeypatch) -> None:
    envs: list[dict[str, str] | None] = []

    def run(argv: list[str], *, env: dict[str, str] | None, **_kw: object) -> subprocess.CompletedProcess[bytes]:
        envs.append(env)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("core.nevia.subprocess.run", run)
    monkeypatch.delenv("AGENT_COMPUTER", raising=False)
    run_cli(["nevia", "ls"], None)
    monkeypatch.setenv("AGENT_COMPUTER", "acc-agent")
    run_cli(["nevia", "ls"], None)
    assert envs[0] is None  # your machine's: its own env, the offer included
    assert envs[1] is not None and envs[1]["NEVIA_NO_UPDATE_CHECK"] == "1" and envs[1]["AGENT_COMPUTER"] == "acc-agent"


def test_a_computer_is_suspended_and_operations_listed_through_the_cli(fake: FakeApi, nevia: Nevia) -> None:
    cli = _cli(nevia)
    c = fake.add("computers", "acc-app")
    nevia.computers.sleep("acc-app")
    assert cli.calls == [["suspend", "acc-app", "--wait"]]  # the command; `sleep` is only its alias
    assert fake.get("computers", "acc-app")["state"] == "sleeping"
    fake.add("operations", "", type="computer.delete", resource=c["id"], resource_type="computer", state="running")
    assert [o["type"] for o in nevia.operations()] == ["computer.delete"]
    assert cli.calls[-1] == ["--json", "ops", "-f", "state=running"]
    assert {o["type"] for o in nevia.operations(running=False)} == {"computer.delete", "sleep"}


def test_a_pooled_uri_goes_into_the_secret_with_its_params(fake: FakeApi, nevia: Nevia) -> None:
    cli = _cli(nevia)
    nevia.databases.create("acc-pg")
    nevia.databases.store_uri("acc-pg", "ACC_DATABASE_URL", pooled=True, params={"prepareThreshold": "0"})
    assert cli.calls[-2][:4] == ["pg", "connect", "acc-pg", "--pooled"]
    [uri] = [c.body["value"] for c in fake.calls if c.op in {"createSecret", "updateSecret"}]
    assert uri == "postgres://app:secret@acc-pg.db.example:6432/app?prepareThreshold=0"  # the pooler's, password in
    assert not any("secret@" in " ".join(c) for c in cli.calls)  # on stdin only


def test_the_workspace_is_made_once() -> None:
    kit = FakeApi(validate=True).client()
    nv = Nevia("m2p", run=FakeCli(kit), _kit=kit)
    assert nv.ensure_workspace() is True
    assert nv.ensure_workspace() is False
    assert [w["name"] for w in kit.workspaces.list()].count("m2p") == 1


def test_a_missing_computer_says_in_which_workspace(pc: Computer) -> None:
    with pytest.raises(RuntimeError, match="no computer m2p-agent in workspace default: `setup` makes it"):
        replace(pc, name="m2p-agent").awake()


def test_a_throttled_sign_in_waits_as_told_and_retries(monkeypatch) -> None:
    calls: list[list[str]] = []
    slept: list[float] = []
    answers = [
        subprocess.CompletedProcess([], 6, b"", b"error: Too many token requests from this address: at most 30 per "
                                                b"60 seconds. Retry after 43 seconds."),
        subprocess.CompletedProcess([], 0, b'{"databases": []}', b""),
    ]  # fmt: skip

    def run(argv: list[str], stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return answers.pop(0)

    monkeypatch.setattr(core_nevia.time, "sleep", slept.append)
    p = Nevia("ws", run=run).cli("pg", "ls")  # a call without retry: refused before it ran, so sent again
    assert p.returncode == 0
    assert len(calls) == 2
    assert slept == [44]


def test_another_busy_error_is_not_retried_unless_asked(monkeypatch) -> None:
    monkeypatch.setattr(core_nevia.time, "sleep", lambda _s: None)
    busy = subprocess.CompletedProcess([], 6, b"", b"error: busy")
    calls: list[int] = []

    def run(argv: list[str], stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
        calls.append(1)
        return busy

    assert Nevia("ws", run=run).cli("pg", "ls").returncode == 6
    assert len(calls) == 1
