"""mongo2pg's binary: the GitHub release's, which both agents' computers run, never a build."""

import hashlib
import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from core.config import BUILD_DIR, Mongo2pg
from core.shell import console, run

BINARY = BUILD_DIR / "mongo2pg"
ASSET = "mongo2pg-linux-x86_64"  # Nevia computers are x86_64


@dataclass
class Builder:
    mongo2pg: Mongo2pg
    fetch: Callable[[Sequence[str]], str] = run  # curl; tests answer for it

    def build(self) -> Path:
        """`build/mongo2pg`: the release's Linux binary, checked against the sha256 GitHub publishes; kept while
        it matches."""
        m = self.mongo2pg
        # curl: it retries, and it reads the whole asset through proxies that cut urllib short.
        curl = ["curl", "-fsSL", "--retry", "3", "--proto", "=https"]
        api = f"https://api.github.com/repos/{m.github}/releases/tags/{m.release}"
        meta = json.loads(self.fetch([*curl, "-H", "Accept: application/vnd.github+json", api]))
        asset = next((a for a in meta.get("assets", []) if a["name"] == ASSET), None)
        if not asset or not str(asset.get("digest", "")).startswith("sha256:"):
            raise RuntimeError(f"release {m.release} of {m.github} has no {ASSET} with a sha256 digest")
        want = asset["digest"].removeprefix("sha256:")
        if BINARY.exists() and hashlib.sha256(BINARY.read_bytes()).hexdigest() == want:
            console.log(f"{BINARY} is {m.release} already")
            return BINARY
        BUILD_DIR.mkdir(exist_ok=True)
        part = BINARY.with_suffix(".part")
        self.fetch([*curl, "-o", str(part), asset["browser_download_url"]])
        if hashlib.sha256(part.read_bytes()).hexdigest() != want:
            part.unlink()
            raise RuntimeError(f"{ASSET} of {m.release}: checksum mismatch")
        part.chmod(0o755)
        part.replace(BINARY)
        console.log(f"downloaded {BINARY} ({m.release}, sha256 {want[:16]})")
        return BINARY

    def install(self, dest: Path) -> str:
        """The binary at `dest` (an agent's `bin/mongo2pg`), executable; its version."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.build(), dest)
        dest.chmod(0o755)
        version = run([str(dest), "--version"]).strip()
        console.log(f"{dest}: {version}")
        return version
