import pytest
from nevia_kit.testing import FakeApi

from core.nevia import Nevia
from m2p.agent import Loop
from m2p.config import Settings


@pytest.fixture
def lp(nevia: Nevia, settings: Settings) -> Loop:
    return Loop(settings, nevia, poll=0)


def _job_succeeds(fake: FakeApi) -> None:
    fake.on_exec(".exit 2>/dev/null", "0\ndone\n")


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
