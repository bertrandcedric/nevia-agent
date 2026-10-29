import pytest
from nevia_kit import Nevia
from nevia_kit.testing import FakeApi

from m2p import config


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
    return fake.client()
