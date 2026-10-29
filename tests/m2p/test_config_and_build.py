import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from core.agent import build
from core.agent.build import ASSET, Builder
from core.local.cli import app as root
from m2p.config import Settings


def test_settings_load(settings: Settings) -> None:
    assert settings.mongo.database == "hello"
    assert settings.mongo2pg.release


def test_a_release_is_required(settings: Settings) -> None:
    data = settings.model_dump()
    data["mongo2pg"]["release"] = ""
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_cli_lists_its_commands() -> None:
    result = CliRunner().invoke(root, ["--help"])
    assert result.exit_code == 0
    assert set(re.findall(r"^│ ([a-z]\S*) ", result.output, re.M)) == {"m2p", "acc"}
    for agent in ("m2p", "acc"):
        listed = set(re.findall(r"^│ ([a-z]\S*) ", CliRunner().invoke(root, [agent, "--help"]).output, re.M))
        assert listed == {"setup", "run", "status", "stop", "clean", "destroy"}


class _Curl:
    """A curl that answers the release's metadata and writes the asset to its -o file."""

    def __init__(self, asset: bytes, digest: str) -> None:
        self.asset, self.digest, self.calls = asset, digest, []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        if "-o" in args:
            Path(args[args.index("-o") + 1]).write_bytes(self.asset)
            return ""
        meta = {"assets": [{"name": ASSET, "digest": self.digest, "browser_download_url": "https://dl.example/m2p"}]}
        return json.dumps(meta)


def test_build_downloads_the_release_binary(settings: Settings, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(build, "BINARY", tmp_path / "mongo2pg")
    monkeypatch.setattr(build, "BUILD_DIR", tmp_path)
    curl = _Curl(b"\x7fELF", f"sha256:{hashlib.sha256(b'\x7fELF').hexdigest()}")
    assert Builder(settings.mongo2pg, curl).build() == tmp_path / "mongo2pg"
    assert (tmp_path / "mongo2pg").read_bytes() == b"\x7fELF"
    assert (
        curl.calls[0][-1] == f"https://api.github.com/repos/pmpetit/mongo2pg/releases/tags/{settings.mongo2pg.release}"
    )
    assert all("--proto" in c for c in curl.calls)
    Builder(settings.mongo2pg, curl).build()  # the binary is kept while it matches
    assert sum("-o" in c for c in curl.calls) == 1


def test_build_refuses_a_binary_that_does_not_match(settings: Settings, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(build, "BINARY", tmp_path / "mongo2pg")
    monkeypatch.setattr(build, "BUILD_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        Builder(settings.mongo2pg, _Curl(b"other", "sha256:" + "0" * 64)).build()
    assert not list(tmp_path.iterdir())


def test_every_nevia_name_carries_its_agent(settings: Settings) -> None:
    assert all(n.startswith("m2p-") for n in (settings.mongo.computer, settings.postgres.name, settings.agent.computer))
    assert settings.secret("PG_URL") == "M2P_PG_URL"
    data = settings.model_dump()
    data["postgres"]["name"] = "hello-pg"
    with pytest.raises(ValidationError, match="starts with m2p-"):
        Settings.model_validate(data)
