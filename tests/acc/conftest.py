import pytest
from nevia_kit.testing import FakeApi

from acc import agent, config
from acc.agent import Agent
from core.nevia import Nevia
from tests.fake_nevia import on_fake


@pytest.fixture
def settings() -> config.Settings:
    return config.load()


@pytest.fixture
def fake(settings: config.Settings) -> FakeApi:
    """The Nevia API in memory, checking every request against its spec, with the agent computer and the stack."""
    api = FakeApi(validate=True)
    for name in (settings.agent.computer, settings.app.computer, settings.mongo.computer):
        api.add("computers", name)
    return api


@pytest.fixture
def nevia(fake: FakeApi) -> Nevia:
    return on_fake(fake)


@pytest.fixture
def ag(nevia: Nevia, settings: config.Settings, tmp_path, monkeypatch) -> Agent:
    monkeypatch.setattr(agent, "RUNS_DIR", tmp_path / "runs")
    return Agent(settings, nevia, poll=0, show=lambda _line: None)
