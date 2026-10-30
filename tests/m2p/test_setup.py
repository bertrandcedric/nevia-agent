import json
from types import SimpleNamespace

import pytest
from nevia_kit.testing import FakeApi

from core.nevia import Nevia
from m2p import agent as loop
from m2p.agent import Loop
from m2p.config import Settings
from tests.fake_nevia import job_ends


@pytest.fixture
def lp(nevia: Nevia, settings: Settings) -> Loop:
    return Loop(settings, nevia, poll=0)


def test_provision_seeds_mongo_on_the_computer(lp: Loop, fake: FakeApi) -> None:
    job_ends(fake)
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


def test_provision_seeds_again_from_scratch_when_data_is_there(lp: Loop, fake: FakeApi) -> None:
    job_ends(fake)
    lp.provision("m2p-demo")
    lp.provision("m2p-demo")
    assert b"MONGO_CACHE_GB=0.25" in fake.files["m2p-demo", "mongo.env"]
    assert sum("python3 ~/seed.py" in s for s in fake.scripts()) == 2
    assert not any("countDocuments" in s for s in fake.scripts())


def test_setup_refuses_while_the_agent_runs(lp: Loop, fake: FakeApi) -> None:
    fake.add("computers", lp.name)
    fake.on_exec("agent.log && ! test -f", "yes\n")  # the agent's job: its log, no exit code yet
    with pytest.raises(RuntimeError, match="the agent is running"):
        lp.setup()
    assert not any("seed.py" in s for s in fake.scripts())


@pytest.fixture
def settled(monkeypatch, fake: FakeApi) -> list[int]:
    """No waiting: for each wait of SNAPSHOT_AGE, how many API calls were made before it."""
    waits: list[int] = []
    monkeypatch.setattr(loop.time, "sleep", lambda s: s >= loop.SNAPSHOT_AGE and waits.append(len(fake.calls)))
    return waits


def _records(fake: FakeApi, seed: str) -> None:
    """`cat ~/seed.json` reads the seed computer's record, as the fake keeps files (a branch has none of them)."""
    fake.on_exec(f"cat ~/{loop.SEED_RECORD}", lambda _a, _b: fake.files.get((seed, loop.SEED_RECORD), b""))


def test_the_source_is_a_fresh_branch_of_the_seed_computer(lp: Loop, fake: FakeApi, settled: list[int]) -> None:
    s = lp.settings
    job_ends(fake)
    _records(fake, s.seed_computer)
    fake.add("computers", s.mongo.computer)  # the last setup's
    old = fake.get("computers", s.mongo.computer)["id"]
    lp.source()
    seed, source = fake.get("computers", s.seed_computer), fake.get("computers", s.mongo.computer)
    assert s.seed_computer == f"{s.mongo.computer}-seed"
    assert source["id"] != old and source["parent"] == seed["id"]
    assert fake.files[s.seed_computer, loop.SEED_RECORD] == lp.seed_record()
    assert json.loads(lp.seed_record())["seed"] == s.seed.model_dump()
    assert seed["state"] == "sleeping"  # until the next setup
    seeded = fake.scripts(s.seed_computer)
    assert any("python3 ~/seed.py" in c for c in seeded)
    assert not any("python3 ~/seed.py" in c for c in fake.scripts(s.mongo.computer))
    assert any("~/.nevia-agent/mongo-up.sh" in c for c in fake.scripts(s.mongo.computer))  # no process in a branch
    # mongod stopped and the disk left alone SNAPSHOT_AGE before the branch.
    stopped = next(i for i, c in enumerate(seeded) if "podman stop -i m2p-mongo" in c and "sync" in c)
    assert stopped == len(seeded) - 1
    [branched] = [i for i, c in enumerate(fake.calls) if c.op == "branchComputer"]
    assert settled and settled[-1] <= branched


def test_a_seed_computer_seeded_alike_is_kept(lp: Loop, fake: FakeApi, settled: list[int]) -> None:
    s = lp.settings
    job_ends(fake)
    _records(fake, s.seed_computer)
    lp.source()
    seed = fake.get("computers", s.seed_computer)["id"]
    lp.source()
    assert fake.get("computers", s.seed_computer)["id"] == seed
    assert sum("python3 ~/seed.py" in c for c in fake.scripts()) == 1
    assert len(fake.ops("branchComputer")) == 2  # a fresh source each time


def test_a_seed_computer_seeded_otherwise_is_made_again(lp: Loop, fake: FakeApi, settled: list[int]) -> None:
    s = lp.settings
    job_ends(fake)
    _records(fake, s.seed_computer)
    lp.source()
    seed = fake.get("computers", s.seed_computer)["id"]
    lp.settings = s.model_copy(update={"seed": s.seed.model_copy(update={"customers": 7000})})
    lp.source()
    assert fake.get("computers", s.seed_computer)["id"] != seed
    assert sum("--customers 7000" in c for c in fake.scripts(s.seed_computer)) == 1
    assert json.loads(fake.files[s.seed_computer, loop.SEED_RECORD])["seed"]["customers"] == 7000


def test_a_seed_that_stopped_halfway_is_made_again(lp: Loop, fake: FakeApi, settled: list[int]) -> None:
    s = lp.settings
    job_ends(fake)
    _records(fake, s.seed_computer)
    fake.add("computers", s.seed_computer)  # no record: its seed never ended
    seed = fake.get("computers", s.seed_computer)["id"]
    lp.source()
    assert fake.get("computers", s.seed_computer)["id"] != seed
    assert fake.files[s.seed_computer, loop.SEED_RECORD] == lp.seed_record()


def test_replacing_the_source_removes_the_agent_s_link_to_it(lp: Loop, fake: FakeApi, settled, monkeypatch) -> None:
    s = lp.settings
    job_ends(fake)
    _records(fake, s.seed_computer)
    fake.add("computers", s.mongo.computer)
    made = SimpleNamespace(id="l1")
    unlinked: list[tuple[str, str]] = []
    monkeypatch.setattr(lp.tunnels, "links", lambda dest: [made] if fake.find("computers", dest) else [])
    monkeypatch.setattr(lp.tunnels, "unlink", lambda dest, link: unlinked.append((dest, link)))
    lp.source()
    assert unlinked == [(s.mongo.computer, "l1")]  # its client on the agent computer holds MONGO_PORT
