import io
import os
import tarfile

import pytest
from nevia_kit.testing import FakeApi
from typer.testing import CliRunner

from acc import local as acc_local
from acc.config import Settings as AccSettings
from core.computer import agent_command, carried
from core.config import WORKSPACE_ENV
from core.doomed import Doomed
from core.local import bootstrap, cli, runtime
from core.local.bootstrap import Bootstrap, Wanted, passed_on
from core.nevia import Nevia
from m2p import local as m2p_local
from m2p.config import Settings


def _boot(nevia: Nevia) -> Bootstrap:
    return m2p_local.Local(Settings.load(), nevia).bootstrap()


def test_secrets_come_from_the_env_prefixed_and_a_missing_one_stops_before_the_computer(
    fake: FakeApi, nevia: Nevia
) -> None:
    boot = _boot(nevia)
    with pytest.raises(RuntimeError, match="AIVEN_AI_API_KEY is not set"):
        boot.up({})
    assert not fake.ops("createComputer")
    boot.wanted.append(Wanted("GITHUB_TOKEN", always=False))
    boot.secrets({"AIVEN_AI_API_KEY": "k", "GITHUB_TOKEN": "t"})
    assert {s["name"] for s in fake.resources["secrets"].values()} == {"M2P_AIVEN_AI_API_KEY", "M2P_GITHUB_TOKEN"}
    boot.secrets({"AIVEN_AI_API_KEY": "k"})  # the token is kept
    with pytest.raises(RuntimeError, match="AIVEN_AI_API_KEY"):
        boot.secrets({"GITHUB_TOKEN": "t"})  # the gateway key is wanted each time


def test_the_key_is_made_once_and_reaches_the_computer_through_the_exec_env_only(fake: FakeApi, nevia: Nevia) -> None:
    fake.add("computers", "m2p-agent")
    boot = _boot(nevia)
    boot.key()
    boot.key()
    [made] = fake.ops("createAccess")
    assert made.body["role"] == "editor" and made.body["name"] == "m2p-agent"
    boot.install_key()
    write = next(body for _, _, body in fake.execs if "AGENT_KEY" in str(body))
    assert write["env"] == {"AGENT_KEY": "{{secrets.M2P_NEVIA_AGENT_KEY}}"}
    assert "umask 077" in write["argv"][-1] and ".config/nevia/keys/m2p-agent.json" in write["argv"][-1]
    assert not fake.env[fake.get("computers", "m2p-agent")["id"]]
    assert not any("PRIVATE KEY" in str(c.body) for c in fake.calls)


def test_a_principal_left_without_its_secret_gets_a_new_key(fake: FakeApi, nevia: Nevia) -> None:
    boot = _boot(nevia)
    pid = nevia.kit.access.add_key_principal("editor", name="m2p-agent", description="m2p agent")["access"][
        "principal"
    ]["id"]
    fake.access[pid]["principal"].update(auth="private_key", name="m2p-agent")
    boot.key()
    assert len(fake.ops("createAccess")) == 1  # the one above: none made again
    [rotated] = fake.ops("rotateKey")
    assert pid in rotated.path
    assert any(s["name"] == "M2P_NEVIA_AGENT_KEY" for s in nevia.secrets.list())


def test_destroy_takes_the_agent_its_principal_and_its_secrets_only(fake: FakeApi, nevia: Nevia) -> None:
    fake.add("computers", "m2p-agent")
    for name in ("M2P_AIVEN_AI_API_KEY", "M2P_NEVIA_AGENT_KEY", "AIVEN_AI_API_KEY", "ACC_NEVIA_AGENT_KEY"):
        nevia.secrets.set(name, "x")
    boot = _boot(nevia)
    ours = nevia.kit.access.add_key_principal("editor", name="m2p-agent", description="m2p agent")["access"]
    fake.access[ours["principal"]["id"]]["principal"].update(auth="private_key", name="m2p-agent")
    theirs = nevia.kit.access.add_key_principal("editor", name="acc-agent", description="acc agent")["access"]
    fake.access[theirs["principal"]["id"]]["principal"].update(auth="private_key", name="acc-agent")
    d = boot.doomed()
    assert d.computers == ["m2p-agent"]
    assert d.secrets == ["M2P_AIVEN_AI_API_KEY", "M2P_NEVIA_AGENT_KEY"]
    assert d.principals == [ours["principal"]["id"]]
    boot.teardown(d)
    assert not boot.doomed()
    assert sorted(s["name"] for s in nevia.secrets.list()) == ["ACC_NEVIA_AGENT_KEY", "AIVEN_AI_API_KEY"]


def test_the_bundle_has_this_code_nevia_kit_the_settings_and_pinned_requirements() -> None:
    with tarfile.open(fileobj=io.BytesIO(runtime.bundle()), mode="r:gz") as tar:
        names = tar.getnames()
        member = tar.extractfile("requirements.txt")
        assert member is not None
        reqs = member.read().decode().split()
    assert (
        "src/core/local/cli.py" in names and "src/core/scripts/lib.sh" in names and "src/nevia_kit/__init__.py" in names
    )
    assert {"src/m2p/agent.py", "src/acc/agent.py", "src/m2p/scripts/branch.sh"} <= set(names)
    assert any(n.startswith("src/nevia_kit-") and n.endswith(".dist-info/METADATA") for n in names)
    assert "prompts/acc/MIGRATE.md" in names and "agent.toml" in names
    assert not any("__pycache__" in n for n in names)
    assert any(r.startswith("typer==") for r in reqs) and any(r.startswith("cryptography==") for r in reqs)
    assert not any(r.startswith("nevia-kit") for r in reqs)  # sent as source: a private repository


def test_remote_commands_run_this_code_on_the_computer() -> None:
    assert (
        agent_command("acc", "run", "task", "--file", "~/x y")
        == "~/.nevia-agent/bin/agent acc remote run task --file '~/x y'"
    )


def test_destroy_lists_both_parts_asks_then_the_agent_and_your_machine_delete(
    fake: FakeApi, nevia: Nevia, monkeypatch
) -> None:
    fake.add("computers", "m2p-agent")
    nevia.secrets.set("M2P_NEVIA_AGENT_KEY", "x")
    boot = _boot(nevia)
    deleted: list[str] = []
    fake.on_exec("remote destroy --yes", lambda _a, _b: deleted.append("theirs") or "deleted computer m2p-mongo\n")
    fake.on_exec("remote destroy", Doomed(["m2p-mongo"], databases=["m2p-pg"]).to_json() + "\n")
    shown: list[str] = []
    monkeypatch.setattr(bootstrap, "show", shown.append)
    monkeypatch.setattr(bootstrap.typer, "confirm", lambda *_a, **_k: (_ for _ in ()).throw(SystemExit(1)))
    with pytest.raises(SystemExit):
        boot.destroy(yes=False)
    assert "computers: m2p-mongo, m2p-agent" in shown[0] and "databases: m2p-pg" in shown[0]
    assert not deleted and fake.find("computers", "m2p-agent")  # nothing deleted before the yes
    boot.destroy(yes=True)
    assert deleted == ["theirs"]  # the agent's part first, then your machine's
    assert not fake.find("computers", "m2p-agent") and not any(nevia.secrets.list())


def test_remote_json_is_the_last_line_after_what_the_command_logged(fake: FakeApi, nevia: Nevia) -> None:
    fake.add("computers", "m2p-agent")
    boot = _boot(nevia)
    fake.on_exec("remote status", 'a log line\n{"done": true}\n', once=True)
    assert boot.remote_json("status") == '{"done": true}'
    fake.on_exec("remote status", "")
    with pytest.raises(RuntimeError, match="printed nothing"):
        boot.remote_json("status")


def test_the_chosen_workspace_is_passed_on_to_the_agent_s_commands_and_jobs(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Paris")
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    assert passed_on() == {"TZ": "Europe/Paris"}
    monkeypatch.setenv(WORKSPACE_ENV, "elsewhere")
    assert passed_on() == {"TZ": "Europe/Paris", WORKSPACE_ENV: "elsewhere"}
    assert carried({"A": "1"}) == {"A": "1", "TZ": "Europe/Paris", WORKSPACE_ENV: "elsewhere"}


def test_agent_workspace_sets_the_override(monkeypatch) -> None:
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    CliRunner().invoke(cli.app, ["--workspace", "elsewhere", "m2p", "--help"])
    assert os.environ.pop(WORKSPACE_ENV) == "elsewhere"


def test_acc_s_ai_gets_a_viewer_principal_of_its_own_made_once_and_destroyed(fake: FakeApi, nevia: Nevia) -> None:
    fake.add("computers", "acc-agent")
    boot = acc_local.Local(AccSettings.load(), nevia).bootstrap()
    boot.key()
    for entry in fake.access.values():  # what FakeApi leaves out of an entry
        entry["principal"]["auth"] = "private_key"
    boot.key()
    made = {c.body["name"]: (c.body["role"], c.body["description"]) for c in fake.ops("createAccess")}
    assert made == {"acc-agent": ("editor", "acc agent"), "acc-agent-ai": ("viewer", "acc agent's AI")}
    assert {"ACC_NEVIA_AGENT_KEY", "ACC_NEVIA_AI_KEY"} <= nevia.secrets.names()
    boot.install_key()
    writes = [(body["env"], body["argv"][-1]) for _, _, body in fake.execs if "AGENT_KEY" in str(body)]
    assert [env for env, _ in writes] == [
        {"AGENT_KEY": "{{secrets.ACC_NEVIA_AGENT_KEY}}"}, {"AGENT_KEY": "{{secrets.ACC_NEVIA_AI_KEY}}"},
    ]  # fmt: skip
    assert ".config/nevia/keys/acc-agent-ai.json" in writes[1][1]
    d = boot.doomed()
    assert sorted(d.principals) == sorted(fake.access) and len(d.principals) == 2
    assert {"ACC_NEVIA_AGENT_KEY", "ACC_NEVIA_AI_KEY"} <= set(d.secrets)
    nevia.secrets.delete("ACC_NEVIA_AI_KEY")  # renewed as the agent's is: its secret deleted
    boot.key()
    assert len(fake.ops("rotateKey")) == 1 and len(fake.ops("createAccess")) == 2
