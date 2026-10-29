import io
import tarfile

import pytest
from nevia_kit.testing import FakeApi

from core.computer import agent_command
from core.local import runtime
from core.local.bootstrap import Bootstrap, Wanted
from m2p import config as m2p_config
from m2p import local as m2p_local


def _boot(fake: FakeApi) -> Bootstrap:
    s = m2p_config.load()
    return m2p_local.bootstrap(s, fake.client())


def test_secrets_come_from_the_env_prefixed_and_a_missing_one_stops_before_the_computer() -> None:
    fake = FakeApi(validate=True)
    boot = _boot(fake)
    with pytest.raises(RuntimeError, match="AIVEN_AI_API_KEY is not set"):
        boot.up({})
    assert not fake.ops("createComputer")
    boot.wanted.append(Wanted("GITHUB_TOKEN", "GITHUB_TOKEN", always=False))
    boot.secrets({"AIVEN_AI_API_KEY": "k", "GITHUB_TOKEN": "t"})
    assert {s["name"] for s in fake.resources["secrets"].values()} == {"M2P_AIVEN_AI_API_KEY", "M2P_GITHUB_TOKEN"}
    boot.secrets({"AIVEN_AI_API_KEY": "k"})  # the token is kept
    with pytest.raises(RuntimeError, match="AIVEN_AI_API_KEY"):
        boot.secrets({"GITHUB_TOKEN": "t"})  # the gateway key is wanted each time


def test_the_key_is_made_once_and_reaches_the_computer_through_the_exec_env_only() -> None:
    fake = FakeApi(validate=True)
    fake.add("computers", "m2p-agent")
    boot = _boot(fake)
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


def test_a_principal_left_without_its_secret_gets_a_new_key() -> None:
    fake = FakeApi(validate=True)
    nevia = fake.client()
    boot = m2p_local.bootstrap(m2p_config.load(), nevia)
    pid = nevia.access.add_key_principal("editor", name="m2p-agent", description="m2p agent")["access"]["principal"][
        "id"
    ]
    fake.access[pid]["principal"].update(auth="private_key", name="m2p-agent")
    boot.key()
    assert len(fake.ops("createAccess")) == 1  # the one above: none made again
    [rotated] = fake.ops("rotateKey")
    assert pid in rotated.path
    assert any(s["name"] == "M2P_NEVIA_AGENT_KEY" for s in nevia.secrets.list())


def test_destroy_takes_the_agent_its_principal_and_its_secrets_only() -> None:
    fake = FakeApi(validate=True)
    fake.add("computers", "m2p-agent")
    nevia = fake.client()
    for name in ("M2P_AIVEN_AI_API_KEY", "M2P_NEVIA_AGENT_KEY", "AIVEN_AI_API_KEY", "ACC_NEVIA_AGENT_KEY"):
        nevia.secrets.set(name, "x")
    boot = m2p_local.bootstrap(m2p_config.load(), nevia)
    ours = nevia.access.add_key_principal("editor", name="m2p-agent", description="m2p agent")["access"]
    fake.access[ours["principal"]["id"]]["principal"].update(auth="private_key", name="m2p-agent")
    theirs = nevia.access.add_key_principal("editor", name="acc-agent", description="acc agent")["access"]
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
    assert "src/prompts/acc/MIGRATE.md" in names and "agent.toml" in names
    assert not any("__pycache__" in n for n in names)
    assert any(r.startswith("typer==") for r in reqs) and any(r.startswith("cryptography==") for r in reqs)
    assert not any(r.startswith("nevia-kit") for r in reqs)  # sent as source: a private repository


def test_remote_commands_run_this_code_on_the_computer() -> None:
    assert (
        agent_command("acc", "run", "task", "--file", "~/x y")
        == "~/.nevia-agent/bin/agent acc remote run task --file '~/x y'"
    )
