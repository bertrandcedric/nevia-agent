import pytest
from pydantic import ValidationError

from m2p.config import Settings


def test_settings_load(settings: Settings) -> None:
    assert settings.mongo.database == "hello"
    assert settings.mongo2pg.release


def test_a_release_is_required(settings: Settings) -> None:
    data = settings.model_dump()
    data["mongo2pg"]["release"] = ""
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_every_nevia_name_carries_its_agent(settings: Settings) -> None:
    assert all(n.startswith("m2p-") for n in (settings.mongo.computer, settings.postgres.name, settings.agent.computer))
    assert settings.secret("PG_URL") == "M2P_PG_URL"
    data = settings.model_dump()
    data["postgres"]["name"] = "hello-pg"
    with pytest.raises(ValidationError, match="starts with m2p-"):
        Settings.model_validate(data)


def test_the_names_made_from_the_settings_stay_apart(settings: Settings) -> None:
    assert settings.seed_computer == "m2p-mongo-seed" and settings.loaded == "m2p-pg-loaded"
    assert settings.is_try("m2p-try12") and not settings.is_try("m2p-try1-loaded") and not settings.is_try("m2p-try")
    data = settings.model_dump()
    data["postgres"]["name"] = "m2p-try7"  # `clean` would take it for a try
    with pytest.raises(ValidationError, match="try's branch"):
        Settings.model_validate(data)
    data = settings.model_dump()
    data["agent"]["computer"] = "m2p-mongo-seed"
    with pytest.raises(ValidationError, match="seed computer"):
        Settings.model_validate(data)
