import pytest
from nevia_kit.testing import FakeApi

from core.nevia import Nevia
from m2p.computer import Computer
from m2p.config import Settings


@pytest.fixture
def pc(nevia: Nevia, settings: Settings) -> Computer:
    return Computer("m2p-demo", nevia, 0, settings=settings)


def test_psql_switches_database_and_keeps_the_uri_remote(pc: Computer, fake: FakeApi) -> None:
    pc.psql("SELECT 1", database="hello")
    cmd = fake.scripts()[0]
    assert 'psql "$DATABASE_URL"' in cmd
    assert "'\\c hello'" in cmd


def test_psql_gets_the_targets_uri_per_command_even_on_the_agent_computer(
    pc: Computer, fake: FakeApi, monkeypatch
) -> None:
    monkeypatch.setenv("AGENT_COMPUTER", pc.name)  # here: still Nevia's exec, for the secret's value
    pc.psql("SELECT 1")
    [(_, _, body)] = [e for e in fake.execs if "psql" in " ".join(e[1])]
    assert body["env"] == {"DATABASE_URL": "{{secrets.M2P_PG_URL}}"}
