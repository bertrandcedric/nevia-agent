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

from core.config import WORKSPACE_ENV, MongoDB
from core.nevia import Nevia, NeviaError
from core.shell import console, redact, run

HOME = ".nevia-agent"  # this project's directory on every computer, under its home
LIB = f"{HOME}/lib.sh"
MONGO_DIR = HOME  # where a MongoDB computer has mongo-host.sh and mongo-up.sh
BOOT_ID = "/proc/sys/kernel/random/boot_id"  # a job's pid file has it: a pid from another boot is stale
BLIPS = 6  # polls in a row `wait_job` waits for a computer that doesn't answer
MONGO_PORT = 27017  # MongoDB's, in its pod (mongo-up.sh's), and the agent computer's end of a link to it


def secret(name: str) -> str:
    """The template a computer's env or an exec env replaces with the workspace secret `name`."""
    return f"{{{{secrets.{name}}}}}"


def carried(env: dict[str, str]) -> dict[str, str]:
    """`env` with what this command got from your machine (`Bootstrap.remote` gives them), for a job that runs
    the agent's code again: its TZ, so that the job's log reads in your time zone, as your clock does, and the
    workspace it was told to use instead of agent.toml's (`config.WORKSPACE_ENV`)."""
    return env | {k: v for k in ("TZ", WORKSPACE_ENV) if (v := os.environ.get(k))}


AGENT_CLI = f"~/{HOME}/bin/agent"  # this code on an agent computer (core/scripts/setup-runtime.sh)
AGENT_BUILD_DIR = f"{HOME}/build"  # BUILD_DIR on an agent computer, under its home: this code is in HOME there


def agent_command(agent: str, *args: str) -> str:
    """`agent <agent> remote <args>` as an agent computer runs it."""
    return " ".join([AGENT_CLI, agent, "remote", *map(shlex.quote, args)])


def bundled(package: str, name: str) -> bytes:
    """A file bundled in `package`, such as `scripts/mongo-up.sh` in `core`."""
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
        """Wake the computer if it sleeps: exec never does. One that isn't there says in which workspace."""
        if (c := self.nevia.computers.find(self.name)) is None:
            raise RuntimeError(f"no computer {self.name} in workspace {self.nevia.workspace}: `setup` makes it")
        if c["state"] == "sleeping":
            console.log(f"waking {self.name}")
            self.nevia.computers.wake(self.name)

    def sleep(self) -> None:
        """Put the computer to sleep: what runs there stops, its disks stay; `awake` wakes it."""
        if (c := self.nevia.computers.find(self.name)) is not None and c["state"] != "sleeping":
            self.nevia.computers.sleep(self.name)
            console.log(f"{self.name} sleeps until a command wakes it")

    def put_lib(self) -> None:
        """`lib.sh`, which every script either agent runs on a computer sources, at LIB."""
        self.upload(bundled("core", "scripts/lib.sh"), LIB, "644")

    def put_tools(self, dest: str, tools: dict[str, tuple[bytes, str]]) -> None:
        """lib.sh, and `tools` (name: data and mode) in `dest` (under the home), replaced as a whole so that tools
        an earlier version had go too."""
        t = f"~/{dest}"
        self.sh(f"chmod -R u+w {t} 2>/dev/null; rm -rf {t} && mkdir -p {t}")
        self.put_lib()
        for name, (data, mode) in tools.items():
            self.upload(data, f"{dest}/{name}", mode)

    def put_mongo(self, mongodb: MongoDB, *, pod: str, container: str, **extra: str) -> None:
        """The MongoDB scripts both agents run (mongo-host.sh, mongo-up.sh), and ~/mongo.env: `[mongodb]`, the
        pod and container names, and `extra` (an app's MongoDB's: MONGO_DB, MONGO_PUBLISH, CHISEL_VERSION)."""
        self.put_lib()
        for name in ("mongo-host.sh", "mongo-up.sh"):
            self.upload(bundled("core", f"scripts/{name}"), f"{MONGO_DIR}/{name}", "700")
        env = {"MONGO_IMAGE": mongodb.image, "MONGO_CACHE_GB": str(mongodb.cache_gb), "MONGO_POD": pod,
               "MONGO_CONTAINER": container, **extra}  # fmt: skip
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
        """A file of the computer (under its home): here, read; elsewhere, in slices through the CLI, each retried
        while the service is busy."""
        if self.here:
            return (Path.home() / path).read_bytes()
        return self.nevia.computers.download(self.name, path)

    @property
    def url(self) -> str:
        return self.nevia.computers.get(self.name)["url"] or ""

    # ------------------------------------------------------------------ jobs

    def _files(self, name: str) -> tuple[str, str, str]:
        return f"{self.JOBS_DIR}/{name}.log", f"{self.JOBS_DIR}/{name}.exit", f"{self.JOBS_DIR}/{name}.pid"

    def start_job(self, name: str, cmd: str, *, env: dict[str, str] | None = None) -> None:
        """Start `cmd` under nohup and return at once; its log and exit code land in JOBS_DIR.

        `cmd` runs in a session of its own (`setsid`), whose id goes in `name.pid` with the boot's, so that
        `stop_job` stops all of it; the wrapper, outside it, writes the exit code however it ends. `env` applies to
        this job only (it may hold `{{secrets.NAME}}` templates) and is never stored on the computer.
        """
        log, rc, pid = self._files(name)
        inner = f"echo $$ $(cat {BOOT_ID} 2>/dev/null) > {pid}; {cmd}"
        wrapped = f"setsid -w bash -lc {shlex.quote(inner)}; echo $? > {rc}"
        self.sh(
            f"mkdir -p {self.JOBS_DIR}; rm -f {rc} {pid}; "
            f"nohup bash -c {shlex.quote(wrapped)} > {log} 2>&1 < /dev/null & echo started",
            retry=False,
            env=env,
        )
        console.log(f"{self.name}: {name} started, log in {log.replace('$HOME', '~')}")

    def stop_job(self, name: str, *, grace: int = 10) -> None:
        """Stop a job started as `name`: TERM to every process of its session, then KILL to what is left after
        `grace` seconds; its wrapper then writes the exit code (143, or 137), which `wait_job` reads as for any
        end. Nothing when it isn't running, or when its pid file is from before the computer last booted."""
        _, rc, pid = self._files(name)
        out = self.sh(
            f"test -f {pid} && ! test -f {rc} || exit 0; read -r s boot < {pid}; "
            'case "$s" in ""|*[!0-9]*|0|1) exit 0;; esac; '  # pkill -s 0 would be this script's own session
            f'[ "$boot" = "$(cat {BOOT_ID} 2>/dev/null)" ] || exit 0; '
            'pkill -TERM -s "$s" || exit 0; '
            f'for _ in $(seq {grace * 2}); do pgrep -s "$s" > /dev/null || break; sleep 0.5; done; '
            'pkill -KILL -s "$s"; '
            f"for _ in $(seq 10); do test -f {rc} && break; sleep 0.2; done; echo stopped",
            retry=False,
        )
        if out.strip():
            console.log(f"{self.name}: {name} stopped")

    def jobs(self, pattern: str = "*") -> list[str]:
        """The jobs started as `pattern` (a glob), running or not."""
        out = self.sh(f"cd {self.JOBS_DIR} 2>/dev/null && ls -1 {pattern}.log 2>/dev/null || true")
        return sorted(line.removesuffix(".log") for line in out.split())

    def job_running(self, name: str) -> bool:
        """Whether a job started as `name` has not written its exit code yet."""
        log, rc, _ = self._files(name)
        return self.sh(f"test -f {log} && ! test -f {rc} && echo yes || true").strip() == "yes"

    def wait_job(self, name: str, *, on_poll: Callable[[], None] | None = None) -> str:
        """Wait for a started job and return the end of its log; raise JobFailed if it failed.

        A dropped exec session does not stop the job; this only polls for the exit file the wrapper writes. A
        computer that doesn't answer for up to BLIPS polls in a row is waited for; after that its error is raised.
        `on_poll` runs at each poll, e.g. to show what the job does; otherwise its last line shows as it changes.
        """
        log, rc, _ = self._files(name)
        last, missed = "", 0
        while True:
            time.sleep(self.poll)
            try:
                status = redact(self.sh(f"cat {rc} 2>/dev/null; echo; tail -n 1 {log} | cut -c1-160")).splitlines()
                if on_poll:
                    on_poll()
            except NeviaError as err:
                missed += 1
                if missed > BLIPS:
                    raise
                console.log(f"{self.name} unreachable ({missed}/{BLIPS}): {str(err).splitlines()[0]}")
                continue
            missed = 0
            code, tail = status[0].strip(), status[-1] if len(status) > 1 else ""
            if not on_poll and tail and tail != last:
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
