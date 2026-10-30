"""A run's results, from the agent computer to `build/<agent>-runs/<run>/`, the same way for both agents: one
archive of the run's directories on the computer (what they leave out aside, and the files over COLLECT_MAX_MB
listed in `skipped.txt` instead), with the agent job's log, downloaded and unpacked here, connection URIs
scrubbed from every text file. Works while the agent runs too.
"""

import io
import shlex
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from core.computer import HOME, Computer, redact
from core.shell import console

COLLECT_MAX_MB = 20
STAGE = f"{HOME}/collect"  # on the computer, under the home: the extra files and skipped.txt
ARCHIVE = f"{STAGE}.tar.gz"


@dataclass(frozen=True)
class Collect:
    """What a run's results are, on the agent computer."""

    dirs: list[str]  # under the home, merged at the root of the results; one that doesn't exist is left out
    skip: tuple[str, ...] = ()  # their paths left out (as `tar --exclude=./PATH` takes them)
    extra: dict[str, str] = field(default_factory=dict)  # name in the results: a file on the computer
    prepare: str = ""  # a command run first, to write more of the results into $STAGE (e.g. a patch)


def _script(c: Collect) -> str:
    q = shlex.quote
    prune = " -o ".join(f"-path ./{q(p)}" for p in c.skip) or "-false"
    excludes = " ".join(f"--exclude=./{q(p)}" for p in c.skip)
    copies = "".join(f'cp {src} "$STAGE"/{q(name)} 2>/dev/null || true; ' for name, src in c.extra.items())
    dirs = " ".join(q(d) for d in c.dirs)
    return (
        f'set -euo pipefail; STAGE=~/{STAGE}; rm -rf "$STAGE" ~/{ARCHIVE}; mkdir -p "$STAGE"; {copies}'
        + (f"( {c.prepare} ) || true; " if c.prepare else "")
        + f': > "$STAGE/skipped.txt"; args=(); for d in {dirs}; do [ -d ~/"$d" ] || continue; '
        f"(cd ~/\"$d\" && find . \\( {prune} \\) -prune -o -type f -size +{COLLECT_MAX_MB}M -printf './%P\\n') "
        '>> "$STAGE/skipped.txt"; args+=(-C ~/"$d" .); done; '
        # Exit 1 is a file that changed while it was read: a log the running agent writes to.
        f'tar -czf ~/{ARCHIVE} --warning=no-file-changed {excludes} -X "$STAGE/skipped.txt" "${{args[@]}}" '
        f'-C "$STAGE" . || [ $? = 1 ]; rm -rf "$STAGE"'
    )


def collect(pc: Computer, c: Collect, dest: Path) -> Path:
    """The results of `c` on `pc`, into `dest`."""
    pc.sh(_script(c))
    data = pc.download(ARCHIVE)
    pc.sh(f"rm -f ~/{ARCHIVE}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")
    for f in (f for f in dest.rglob("*") if f.is_file()):
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            continue
        if (clean := redact(text)) != text:
            f.write_text(clean)
    console.log(f"{pc.name}: {len(data) / 1024**2:.1f} MiB of results in {dest}")
    return dest
