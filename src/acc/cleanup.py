"""What runs left, and the stack, for `clean` and `destroy` on the agent computer: found by the names a run makes
(`RunNames`) and those in agent.toml, and deleted (`core.doomed.delete`)."""

import shlex
from dataclasses import dataclass

from acc.computer import JOBS_DIR, Computer
from acc.config import Settings
from acc.run import run_dir_of
from acc.stack import RunNames
from core.doomed import Doomed, delete
from core.nevia import Nevia
from core.shell import console


@dataclass
class Cleanup:
    settings: Settings
    nevia: Nevia
    poll: float = 10.0

    def doomed(self, run: str | None = None) -> Doomed:
        """What a run left (all runs' when None): sandbox computers, the checkpoint, databases, secrets, files."""
        s = self.settings
        app, names = s.app.computer, RunNames(s, run or "")
        computers = [c["name"] for c in self.nevia.computers.list()]
        sandboxes = names.computers(computers)
        checkpoints = []
        if app in computers:
            for c in self.nevia.checkpoints.list(app):
                if names.matches(k := c["name"] or "", "checkpoint"):
                    checkpoints.append((app, k))
        dbs = [d["name"] for d in self.nevia.databases.list()]
        databases = sorted(d for d in dbs if names.matches(d, "pg", "dev_db"))
        secrets = sorted(x for x in self.nevia.secrets.names() if names.matches(x, "pg_secret", "dev_secret"))
        dirs = [run_dir_of(s.agent.dir, run)] if run else [f"{s.agent.dir}/runs"]
        jobs = f"*-{run}.*" if run else "*-[0-9]*.*"
        return Doomed(sandboxes, sorted(checkpoints), databases, secrets, dirs=dirs, jobs=jobs)

    def doomed_all(self) -> Doomed:
        """What the agent deletes in `destroy`: every run's, then the stack and the secrets it made. The agent
        computer, its principal and the secrets from `.env` are your machine's to delete (`Bootstrap`)."""
        s, runs = self.settings, self.doomed()
        fixed = [s.app.computer, s.mongo.computer]
        computers = [*runs.computers, *(c for c in fixed if self.nevia.computers.exists(c))]
        databases = [*runs.databases, *([s.postgres.name] if self.nevia.databases.exists(s.postgres.name) else [])]
        fields = (s.postgres.secret, s.mongo.password_secret, s.chisel.password_secret)
        names = [s.secret(f) for f in fields]
        have = self.nevia.secrets.names()
        return Doomed(computers, runs.checkpoints, databases, [*runs.secrets, *(n for n in names if n in have)])

    def left(self, run: str) -> list[str]:
        """What the run left on Nevia, for its report."""
        d = self.doomed(run)
        return ([f"computer `{c}`" for c in d.computers] + [f"checkpoint `{k}` of `{c}`" for c, k in d.checkpoints]
                + [f"database `{x}`" for x in d.databases] + [f"secret `{x}`" for x in d.secrets])  # fmt: skip

    def delete(self, d: Doomed) -> None:
        """Delete what `d` lists (`core.doomed.delete`): the sandboxes before the checkpoint they come from."""
        delete(self.nevia, d, files=self._delete_files)

    def _delete_files(self, d: Doomed) -> None:
        name = self.settings.agent.computer
        if self.nevia.computers.exists(name):
            pc = Computer(name, self.nevia, self.poll)
            pc.awake()
            paths = " ".join(f"~/{shlex.quote(p)}" for p in d.dirs)
            pc.sh(f"chmod -R u+w {paths} 2>/dev/null; rm -rf {paths} {JOBS_DIR}/{d.jobs}")
            console.log(f"deleted {', '.join(d.dirs)} on {name}")
