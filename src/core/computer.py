"""Running things on a Nevia computer: bundled scripts and background jobs, for either agent.

On the agent computer itself (`here`), commands and files are local: a subprocess, a file write. Only a command
with a secret in its env goes through Nevia's exec, the one way a `{{secrets.NAME}}` template gets its value.
"""

import os
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import ClassVar

from core.nevia import Nevia, NeviaError
from core.shell import console, redact, run

DOWNLOAD_TRIES = 3
LIB = ".nevia-agent/lib.sh"  # on every computer, under its home
MONGO_DIR = ".nevia-agent"  # where a MongoDB computer has mongo-host.sh and mongo-up.sh


def secret(name: str) -> str:
    """The template a computer's env or an exec env replaces with the workspace secret `name`."""
    return f"{{{{secrets.{name}}}}}"


AGENT_CLI = "~/.nevia-agent/bin/agent"  # this code on an agent computer (core/scripts/setup-runtime.sh)


def agent_command(agent: str, *args: str) -> str:
    """`agent <agent> remote <args>` as an agent computer runs it."""
    return " ".join([AGENT_CLI, agent, "remote", *map(shlex.quote, args)])


def bundled(package: str, name: str) -> bytes:
    """A file bundled in `package`, such as `mongo-up.sh` in an agent's `remote`."""
    return resources.files(package).joinpath(name).read_bytes()


class JobFailed(RuntimeError):
    pass


@dataclass
class Computer:
    """A computer an agent works on. Each agent's subclass names where its scripts are bundled and its jobs dir."""

    name: str
    nevia: Nevia
    poll: float = 10.0

    PACKAGE: ClassVar[str]  # the agent's package: `put` finds the scripts in its scripts/
    JOBS_DIR: ClassVar[str]  # absolute: job commands often cd elsewhere first

    def sh(self, cmd: str, *, retry: bool = True, env: dict[str, str] | None = None) -> str:
        """Run a bash script and return its output; `retry` repeats it after a dropped connection, so use False
        for non-repeatable ones. Here, without a secret in `env`, a local subprocess."""
        if self.here and not any("{{secrets." in v for v in (env or {}).values()):
            return run(["bash", "-lc", cmd], cwd=Path.home(), env=env)
        return self.nevia.computers.sh(self.name, cmd, retry=retry, env=env)

    def upload(self, data: bytes, path: str, mode: str = "600") -> None:
        """`data` at `path` (under the home), mode `mode`: here, a file written beside it then moved in place, as
        nevia-kit's upload does, so that a read-only file (a tool, the guide) is replaced too."""
        if self.here:
            target = Path.home() / path
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_name(f".{target.name}.part")
            part.write_bytes(data)
            part.chmod(int(mode, 8))
            part.replace(target)
        else:
            self.nevia.computers.upload(self.name, data, path, mode)

    @property
    def here(self) -> bool:
        """Whether this code runs on this computer: the agent's own, where `bin/agent` sets AGENT_COMPUTER."""
        return os.environ.get("AGENT_COMPUTER") == self.name

    def awake(self) -> None:
        """Wake the computer if it sleeps: exec never does."""
        if self.nevia.computers.get(self.name)["state"] == "sleeping":
            console.log(f"waking {self.name}")
            self.nevia.computers.wake(self.name)

    def put_lib(self) -> None:
        """`lib.sh`, which every script either agent runs on a computer sources, at LIB."""
        self.upload(bundled("core", "scripts/lib.sh"), LIB, "644")

    def put_mongo(self, env: dict[str, str]) -> None:
        """The MongoDB scripts both agents run (mongo-host.sh, mongo-up.sh), and ~/mongo.env with `env`."""
        self.put_lib()
        for name in ("mongo-host.sh", "mongo-up.sh"):
            self.upload(bundled("core", f"scripts/{name}"), f"{MONGO_DIR}/{name}", "700")
        lines = "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items())
        self.upload(lines.encode(), "mongo.env", "600")

    def mongo_up(self, image: str) -> str:
        """MongoDB started on this computer from the scripts `put_mongo` sent; returns the end of its log."""
        self.sh(f"bash ~/{MONGO_DIR}/mongo-host.sh {shlex.quote(image)}")
        return self.run_job("mongo-up", f"bash ~/{MONGO_DIR}/mongo-up.sh")

    def put(self, name: str, dest: str, mode: str = "700") -> None:
        """Upload one of this agent's scripts (`<agent>/scripts/<name>`) to `dest` (relative to the remote home)."""
        self.upload(bundled(self.PACKAGE, f"scripts/{name}"), dest, mode)

    def download(self, path: str) -> bytes:
        """A file of the computer (under its home), read again from the start if a slice fails: nevia-kit's
        download reads it in many execs, and one that answers without a result fails it with a bare KeyError."""
        if self.here:
            return (Path.home() / path).read_bytes()
        for attempt in range(1, DOWNLOAD_TRIES + 1):
            try:
                return self.nevia.computers.download(self.name, path)
            except (KeyError, NeviaError) as err:
                console.log(f"{self.name}: download of ~/{path} failed ({err!r}), try {attempt} of {DOWNLOAD_TRIES}")
                time.sleep(self.poll)
        raise NeviaError(f"could not download ~/{path} from {self.name} after {DOWNLOAD_TRIES} tries")

    @property
    def url(self) -> str:
        return self.nevia.computers.get(self.name)["url"] or ""

    # ------------------------------------------------------------------ jobs

    def _files(self, name: str) -> tuple[str, str]:
        return f"{self.JOBS_DIR}/{name}.log", f"{self.JOBS_DIR}/{name}.exit"

    def start_job(self, name: str, cmd: str, *, env: dict[str, str] | None = None) -> None:
        """Start `cmd` under nohup and return at once; its log and exit code land in JOBS_DIR.

        `env` applies to this job only (it may hold `{{secrets.NAME}}` templates) and is never stored on the
        computer.
        """
        log, rc = self._files(name)
        wrapped = f"{cmd}; echo $? > {rc}"
        self.sh(
            f"mkdir -p {self.JOBS_DIR}; rm -f {rc}; "
            f"nohup bash -lc {shlex.quote(wrapped)} > {log} 2>&1 < /dev/null & echo started",
            retry=False,
            env=env,
        )
        console.log(f"{self.name}: {name} started, log in {log.replace('$HOME', '~')}")

    def job_running(self, name: str) -> bool:
        """Whether a job started as `name` has not written its exit code yet."""
        log, rc = self._files(name)
        return self.sh(f"test -f {log} && ! test -f {rc} && echo yes || true").strip() == "yes"

    def wait_job(self, name: str, *, on_poll: Callable[[], None] | None = None) -> str:
        """Wait for a started job and return the end of its log; raise JobFailed if it failed.

        A dropped exec session does not stop the job; this only polls for the exit file the wrapper writes.
        `on_poll` runs at each poll, e.g. to show what the job does; otherwise its last line shows as it changes.
        """
        log, rc = self._files(name)
        last = ""
        while True:
            time.sleep(self.poll)
            status = redact(self.sh(f"cat {rc} 2>/dev/null; echo; tail -n 1 {log} | cut -c1-160")).splitlines()
            code, tail = status[0].strip(), status[-1] if len(status) > 1 else ""
            if on_poll:
                on_poll()
            elif tail and tail != last:
                console.log(f"{name}: {tail}")
                last = tail
            if code:
                break
        out = redact(self.sh(f"tail -n 300 {log}"))
        if code != "0":
            raise JobFailed(f"{name} on {self.name} exited {code}:\n" + "\n".join(out.splitlines()[-15:]))
        return out

    def follow_job(self, name: str, show: Callable[[str], None]) -> str:
        """Wait for a started job like `wait_job`, showing every line of its log as it comes, not only the last
        one at each poll: what the agent does on its computer, shown on your machine."""
        log, seen = self._files(name)[0], [0]

        def new_lines() -> None:
            lines = self.sh(f"tail -n +{seen[0] + 1} {log} 2>/dev/null || true").splitlines()
            seen[0] += len(lines)
            for line in lines:
                show(redact(line))

        try:
            return self.wait_job(name, on_poll=new_lines)
        except JobFailed as err:
            new_lines()
            # Its log is shown above already: the first line only, not its end again.
            raise JobFailed(str(err).splitlines()[0]) from None
        finally:
            new_lines()

    def run_job(self, name: str, cmd: str, *, env: dict[str, str] | None = None) -> str:
        """Run `cmd` under nohup, wait for it, and return the end of its log."""
        self.start_job(name, cmd, env=env)
        return self.wait_job(name)
