from collections.abc import Callable

import pytest
from nevia_kit.testing import FakeApi

from acc import config, run
from acc.agent import Agent
from core.nevia import Nevia


@pytest.fixture
def settings() -> config.Settings:
    return config.Settings.load()


@pytest.fixture
def fake(make_fake: Callable[..., FakeApi], settings: config.Settings) -> FakeApi:
    """With the agent computer and the stack."""
    return make_fake(settings.agent.computer, settings.app.computer, settings.mongo.computer)


@pytest.fixture
def ag(nevia: Nevia, settings: config.Settings, tmp_path, monkeypatch) -> Agent:
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    return Agent(settings, nevia, poll=0, show=lambda _line: None)
