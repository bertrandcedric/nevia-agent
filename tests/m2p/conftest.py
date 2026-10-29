import pytest
from nevia_kit.testing import FakeApi

from core.nevia import Nevia
from m2p import config
from tests.fake_nevia import on_fake


@pytest.fixture
def settings() -> config.Settings:
    return config.load()


@pytest.fixture
def fake() -> FakeApi:
    """The Nevia API in memory, checking every request against its spec."""
    api = FakeApi(validate=True)
    api.add("computers", "m2p-demo")
    api.add("databases", "m2p-pg")
    return api


@pytest.fixture
def nevia(fake: FakeApi) -> Nevia:
    return on_fake(fake)
