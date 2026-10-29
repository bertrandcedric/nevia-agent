import pytest
from nevia_kit import Nevia
from nevia_kit.testing import FakeApi

from m2p.agent import Loop
from m2p.config import Settings


@pytest.fixture
def lp(nevia: Nevia, settings: Settings) -> Loop:
    return Loop(settings, nevia, poll=0)


def _job_succeeds(fake: FakeApi) -> None:
    fake.on_exec(".exit 2>/dev/null", "0\ndone\n")


def test_the_cli_signs_in_with_the_key_file_setup_put_there(lp: Loop, fake: FakeApi) -> None:
    fake.on_exec("echo $HOME", "/home/nevia\n")
    lp.install_cli("m2p-demo")
    scripts = fake.scripts()
    assert any("get.nevia.cloud" in s and "NEVIA_VERSION=0.1.12" in s for s in scripts)
    assert any("auth use-key-file /home/nevia/.config/nevia/keys/m2p-agent.json" in s for s in scripts)
    assert any("workspace use default" in s for s in scripts)
    assert not any(k.startswith("NEVIA_") for k in fake.env[fake.get("computers", "m2p-demo")["id"]])
    assert not any("PRIVATE KEY" in str(c.body) for c in fake.calls)


def test_provision_seeds_an_empty_mongo_on_the_computer(lp: Loop, fake: FakeApi) -> None:
    _job_succeeds(fake)
    fake.on_exec("countDocuments", "0\n")
    lp.provision("m2p-demo")
    assert fake.files["m2p-demo", "seed.py"].startswith(b'"""Fill the database hello')
    scripts = fake.scripts()
    up = next(i for i, s in enumerate(scripts) if "~/.nevia-agent/mongo-up.sh" in s)
    seed = next(i for i, s in enumerate(scripts) if "python3 ~/seed.py --db hello --customers 5000" in s)
    assert up < seed
    # In Mongo's network namespace: the job wrapper quotes the command again, so look at its parts.
    assert "podman unshare nsenter" in scripts[seed]
    assert "{{.State.Pid}}" in scripts[seed]
    assert "m2p-mongo" in scripts[seed]


def test_provision_keeps_the_data_there(lp: Loop, fake: FakeApi) -> None:
    _job_succeeds(fake)
    fake.on_exec("countDocuments", "5000\n")
    lp.provision("m2p-demo")
    assert b"MONGO_CACHE_GB=0.25" in fake.files["m2p-demo", "mongo.env"]
    assert not any("seed.py" in s for s in fake.scripts())
