from collections.abc import Callable

import pytest
from nevia_kit.testing import FakeApi

from m2p import config


@pytest.fixture
def settings() -> config.Settings:
    return config.Settings.load()


@pytest.fixture
def fake(make_fake: Callable[..., FakeApi]) -> FakeApi:
    """With m2p's MongoDB computer and its PostgreSQL."""
    api = make_fake("m2p-demo")
    api.add("databases", "m2p-pg")
    return api
