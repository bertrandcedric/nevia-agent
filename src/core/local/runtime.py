"""This code, on an agent computer: everything but making the agent computer runs there.

`bundle` packs this package, nevia-kit (a private repository the computer can't clone, so its source goes
along) and `agent.toml`, with the dependencies pinned at the versions installed here; `setup-runtime.sh`
installs them on the computer with uv, under `~/.nevia-agent`, and writes `~/.nevia-agent/bin/agent`, the
`agent` CLI there, signed in with the computer's own key file.
"""

import io
import re
import shlex
import tarfile
import tomllib
from importlib import metadata
from pathlib import Path

import nevia_kit

from core.config import CONFIG_FILE, ROOT, Runtime

HOME = ".nevia-agent"  # under the computer user's home
BUNDLE = f"{HOME}/bundle.tgz"
SRC = Path(__file__).resolve().parents[2]  # src/, where this project's packages are
PACKAGES = ("core", "m2p", "acc", "prompts")
SHIPPED = "nevia-kit"  # sent as source, not installed from an index
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _name(requirement: str) -> str:
    m = _NAME.match(requirement)
    if not m:
        raise ValueError(f"not a requirement: {requirement!r}")
    return m[1]


def requirements() -> list[str]:
    """`name==version` of every dependency, as installed here: this project's, nevia-kit's, and theirs. Extras
    are left out; a dependency for another platform, not installed here, too."""
    roots = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    todo, pinned = list(roots), {}
    while todo:
        req = todo.pop()
        name, _, marker = req.partition(";")
        if "extra" in marker:
            continue
        key = _name(name).lower().replace("_", "-")
        if key in pinned:
            continue
        try:
            dist = metadata.distribution(key)
        except metadata.PackageNotFoundError:
            continue  # another platform's
        pinned[key] = "" if key == SHIPPED else f"{key}=={dist.version}" + (f"; {marker.strip()}" if marker else "")
        todo.extend(dist.requires or [])
    return sorted(v for v in pinned.values() if v)


def _add_tree(tar: tarfile.TarFile, src: Path, arcname: str) -> None:
    for f in sorted(src.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            tar.add(f, f"{arcname}/{f.relative_to(src)}")


def _add_metadata(tar: tarfile.TarFile, dist: str) -> None:
    """A distribution's `*.dist-info`: nevia-kit reads its own version from it."""
    d = metadata.distribution(dist)
    for f in d.files or []:
        if f.parts[0].endswith(".dist-info"):
            tar.add(Path(str(d.locate_file(f))), f"src/{f}")


def bundle() -> bytes:
    """The .tar.gz `setup-runtime.sh` installs: `src/` (this package, and nevia-kit with its metadata),
    `agent.toml`, `requirements.txt`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for package in PACKAGES:
            _add_tree(tar, SRC / package, f"src/{package}")
        _add_tree(tar, Path(nevia_kit.__file__).parent, "src/nevia_kit")
        _add_metadata(tar, SHIPPED)
        tar.add(CONFIG_FILE, "agent.toml")
        reqs = ("\n".join(requirements()) + "\n").encode()
        info = tarfile.TarInfo("requirements.txt")
        info.size = len(reqs)
        tar.addfile(info, io.BytesIO(reqs))
    return buf.getvalue()


def setup_command(runtime: Runtime, computer: str, key_path: str) -> str:
    return " ".join(
        ["bash", f"~/{HOME}/setup-runtime.sh", *map(shlex.quote, [runtime.uv, runtime.python, computer, key_path])]
    )
