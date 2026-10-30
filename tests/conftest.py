"""What every test shares: the Nevia API in memory behind the fake `nevia`, and a computer of no agent's for core's
tests. The agents' conftests seed `fake` with their computers and add their `settings`."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import pytest
from nevia_kit.testing import FakeApi

from core.computer import Computer
from core.nevia import Nevia
from tests.fake_nevia import on_fake


@pytest.fixture
def make_fake() -> Callable[..., FakeApi]:
    """The Nevia API in memory, checking every request against its spec, with `computers` already there."""

    def make(*computers: str) -> FakeApi:
        api = FakeApi(validate=True)
        for name in computers:
            api.add("computers", name)
        return api

    return make


@pytest.fixture
def fake(make_fake: Callable[..., FakeApi]) -> FakeApi:
    return make_fake()


@pytest.fixture
def nevia(fake: FakeApi) -> Nevia:
    return on_fake(fake)


@dataclass
class Pc(Computer):
    """A computer of core's own: its scripts are core's."""

    PACKAGE: ClassVar[str] = "core"
    JOBS_DIR: ClassVar[str] = "$HOME/.pc-jobs"


@pytest.fixture
def pc(fake: FakeApi, nevia: Nevia) -> Computer:
    fake.add("computers", "pc")
    return Pc("pc", nevia, 0)
