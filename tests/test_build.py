import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from core.agent import build
from core.agent.build import ASSET, Builder
from core.config import Mongo2pg

RELEASE = Mongo2pg(repo="https://github.com/o/mongo2pg.git", release="mongo2pg-v1", sample_percent=1)


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


@pytest.fixture
def build_dir(tmp_path: Path, monkeypatch) -> Path:
    """The build's dir, and the binary in it, in a tmp dir."""
    monkeypatch.setattr(build, "BINARY", tmp_path / "mongo2pg")
    monkeypatch.setattr(build, "BUILD_DIR", tmp_path)
    return tmp_path


def test_build_downloads_the_release_binary(build_dir: Path) -> None:
    curl = _Curl(b"\x7fELF", f"sha256:{hashlib.sha256(b'\x7fELF').hexdigest()}")
    assert Builder(RELEASE, curl).build() == build_dir / "mongo2pg"
    assert (build_dir / "mongo2pg").read_bytes() == b"\x7fELF"
    assert curl.calls[0][-1] == f"https://api.github.com/repos/o/mongo2pg/releases/tags/{RELEASE.release}"
    assert all("--proto" in c for c in curl.calls)
    Builder(RELEASE, curl).build()  # the binary is kept while it matches
    assert sum("-o" in c for c in curl.calls) == 1


def test_build_refuses_a_binary_that_does_not_match(build_dir: Path) -> None:
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        Builder(RELEASE, _Curl(b"other", "sha256:" + "0" * 64)).build()
    assert not list(build_dir.iterdir())
