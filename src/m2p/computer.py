"""m2p's computers: its bundled scripts and jobs, Mongo and psql."""

import shlex
from dataclasses import dataclass, field
from typing import ClassVar

from core.computer import Computer as _Computer
from core.computer import JobFailed, redact, secret
from m2p.config import Settings

__all__ = ["JOBS_DIR", "MONGO_CONTAINER", "Computer", "JobFailed", "redact"]

PACKAGE = "m2p"  # its scripts are in m2p/scripts/
JOBS_DIR = "$HOME/.m2p-jobs"  # absolute: job commands often cd elsewhere first
MONGO_CONTAINER = "m2p-mongo"  # from mongo-up.sh


@dataclass
class Computer(_Computer):
    settings: Settings = field(kw_only=True)
    poll: float = 20.0

    PACKAGE: ClassVar[str] = PACKAGE
    JOBS_DIR: ClassVar[str] = JOBS_DIR

    # ------------------------------------------------------------------ mongo

    def mongo(self, js: str, db: str | None = None) -> str:
        db = db or self.settings.mongo.database
        return self.sh(f"podman exec {MONGO_CONTAINER} mongosh --quiet {db} --eval {shlex.quote(js)}")

    # ------------------------------------------------------------------ postgres

    def psql(self, sql: str, *, database: str | None = None) -> str:
        """Run SQL with psql on the computer, connected to the target (its URI's secret, in this command's env:
        through Nevia's exec even here, the one way the secret gets its value), then switched to `database`.
        The URI never leaves the computer."""
        s = self.settings
        switch = f"-c {shlex.quote(chr(92) + 'c ' + database)} " if database else ""
        return self.sh(
            f'psql "$DATABASE_URL" -X -Atq -v ON_ERROR_STOP=1 {switch}-c {shlex.quote(sql)}',
            env={"DATABASE_URL": secret(s.secret(s.postgres.secret))},
        )

    def memory(self) -> dict[str, str]:
        out = self.sh(
            "echo avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)MB"
            " load=$(cut -d' ' -f1 /proc/loadavg)"
            " oom_kills=$(dmesg 2>/dev/null | grep -c 'Killed process')"
            " job=$(pgrep -a mongo2pg | awk '{print $3}' | head -1)"
            f" mongo=$(podman inspect -f '{{{{.State.Status}}}}' {MONGO_CONTAINER} 2>/dev/null)"
        )
        return dict(kv.split("=", 1) for kv in out.split() if "=" in kv)
