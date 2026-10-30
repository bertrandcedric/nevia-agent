import pytest
from nevia_kit.testing import FakeApi

from core.computer import Computer
from core.nevia import Nevia
from m2p.config import Settings
from m2p.local import Local
from m2p.run import AGENT_JOB
from tests.fake_nevia import job_ends


@pytest.fixture
def lp(nevia: Nevia, fake: FakeApi) -> Local:
    s = Settings.load()
    fake.add("computers", s.agent.computer)
    return Local(s, nevia)


def _asleep(lp: Local, fake: FakeApi) -> bool:
    return (fake.find("computers", lp.name) or {})["state"] == "sleeping"


def test_the_agent_computer_sleeps_after_a_run_even_one_that_failed(lp: Local, fake: FakeApi) -> None:
    job_ends(fake)
    with pytest.raises(RuntimeError, match="collect failed"), lp.asleep_after(AGENT_JOB):
        raise RuntimeError("collect failed")
    assert _asleep(lp, fake)


def test_a_ctrl_c_leaves_it_awake_the_run_goes_on(lp: Local, fake: FakeApi) -> None:
    with pytest.raises(KeyboardInterrupt), lp.asleep_after(AGENT_JOB):
        raise KeyboardInterrupt
    assert not _asleep(lp, fake)


def test_a_job_still_running_leaves_it_awake(lp: Local, fake: FakeApi) -> None:
    fake.on_exec(f"{AGENT_JOB}.log && ! test -f", "yes\n")
    with lp.asleep_after(AGENT_JOB):
        pass
    assert not _asleep(lp, fake)


def test_a_failed_sleep_only_says_so(lp: Local, fake: FakeApi, monkeypatch, capsys) -> None:
    def refuse(self: Computer) -> None:
        raise RuntimeError("busy")

    monkeypatch.setattr(Computer, "sleep", refuse)
    with lp.asleep_after(AGENT_JOB):
        pass
    assert "not put to sleep" in capsys.readouterr().err


def test_status_shows_nevia_s_running_operations_by_name_and_nothing_when_none(lp: Local, fake: FakeApi) -> None:
    assert lp.operations() == []
    c = fake.get("computers", lp.name)
    db = fake.add("databases", "m2p-pg")
    fake.add("operations", "", type="computer.wake", resource=c["id"], resource_type="computer", state="finished")
    fake.add("operations", "", type="database.branch", resource=db["id"], resource_type="database", state="running",
             created_at="2026-10-30T09:15:00Z")  # fmt: skip
    fake.add("operations", "", type="checkpoint.create", resource="k1", resource_type="checkpoint", state="running")
    lines = lp.operations()
    assert len(lines) == 2  # running only
    assert lines[0].startswith("running on Nevia: database.branch of database m2p-pg, since 2026-10-30 ")
    assert lines[1].startswith("running on Nevia: checkpoint.create of checkpoint k1, since ")  # no name: its id
