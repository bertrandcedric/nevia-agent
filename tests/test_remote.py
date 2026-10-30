import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from nevia_kit.testing import FakeApi

from core.agent import ai
from core.computer import BLIPS, Computer, JobFailed, bundled, redact
from core.local.collect import Collect, _script
from core.nevia import NeviaError
from tests.fake_nevia import job_ends


def test_redact_hides_uri_passwords() -> None:
    line = "connecting to postgres://avnadmin:s3cr3t@host:5432/db?sslmode=require"
    assert redact(line) == "connecting to postgres://avnadmin:***@host:5432/db?sslmode=require"


def test_bundled_scripts_are_packaged() -> None:
    assert bundled("m2p", "scripts/branch.sh")
    for name in ["lib.sh", "mongo-host.sh", "mongo-up.sh", "setup-runtime.sh", *ai.TOOLS]:  # both agents'
        assert bundled("core", f"scripts/{name}")


def test_job_waits_for_the_exit_file(pc: Computer, fake: FakeApi) -> None:
    fake.on_exec("cat $HOME/.pc-jobs/export.exit", "\nexporting\n", once=True)
    job_ends(fake, "line 1\ndone\n")
    assert pc.run_job("export", "./export.sh").endswith("done\n")
    started = fake.scripts()[0]
    assert "nohup" in started
    assert "echo $? > $HOME/.pc-jobs/export.exit" in started


def test_failed_job_raises_with_the_log_tail(pc: Computer, fake: FakeApi) -> None:
    job_ends(fake, "postgres://u:pw@h/db\nError: boom\n", code="1")
    with pytest.raises(JobFailed, match="exited 1") as err:
        pc.run_job("import", "./import.sh")
    assert "pw" not in str(err.value)


def test_follow_job_shows_every_line_of_the_log_not_only_the_last(pc: Computer, fake: FakeApi) -> None:
    log = ["[seed] 5000 customers", "[seed]   bool-string: 4", "[seed]   date-string: 0", "agent ready"]
    polls = iter([2, 4])  # the log has grown to 2 lines at the first poll, 4 at the second

    def tail(argv: list[str], _body: object) -> str:
        start = int(argv[-1].split("tail -n +")[1].split()[0]) - 1
        return "".join(f"{line}\n" for line in log[start : upto[0]])

    upto = [0]

    def exit_file(_argv: list[str], _body: object) -> str:
        upto[0] = next(polls, 4)
        return "0\nagent ready\n" if upto[0] == 4 else "\n[seed]\n"

    fake.on_exec(".exit 2>/dev/null; echo", exit_file)
    fake.on_exec("tail -n +", tail)
    shown: list[str] = []
    pc.follow_job("setup", shown.append)
    assert shown == log


def test_collect_packs_the_dirs_the_extra_files_and_a_prepared_file_the_same_way_for_both() -> None:
    c = Collect(["acc/runs/1", ".nevia-agent/build/acc-runs/1"], ("repo",), {"agent-job.log": "$HOME/j.log"},
                'echo x > "$STAGE/change.patch"')  # fmt: skip
    script = _script(c)
    assert subprocess.run(["bash", "-n", "-c", script], check=False).returncode == 0
    assert "for d in acc/runs/1 .nevia-agent/build/acc-runs/1" in script
    assert "--exclude=./repo" in script and "-path ./repo" in script
    assert 'cp $HOME/j.log "$STAGE"/agent-job.log' in script
    assert '( echo x > "$STAGE/change.patch" ) || true' in script


def test_an_upload_here_replaces_a_read_only_file(pc: Computer, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_COMPUTER", pc.name)
    monkeypatch.setenv("HOME", str(tmp_path))
    guide = tmp_path / "loop" / "AGENTS.md"
    pc.upload(b"first", "loop/AGENTS.md", "444")
    pc.upload(b"second", "loop/AGENTS.md", "444")
    assert guide.read_bytes() == b"second"
    assert oct(guide.stat().st_mode & 0o777) == "0o444"
    assert [p.name for p in guide.parent.iterdir()] == ["AGENTS.md"]


@pytest.fixture
def here(pc: Computer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Computer:
    """The agent computer as this machine, in a tmp HOME, with a `setsid` where it has none (macOS)."""
    if not shutil.which("setsid"):
        bin_ = tmp_path / "bin"
        bin_.mkdir()
        shim = bin_ / "setsid"
        shim.write_text(f"#!{sys.executable}\nimport os, sys\nos.setsid()\nos.execvp(sys.argv[2], sys.argv[2:])\n")
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_COMPUTER", pc.name)
    return replace(pc, poll=0.1)


def test_a_job_runs_in_a_session_of_its_own_and_its_wrapper_writes_the_exit_code(pc: Computer, fake: FakeApi) -> None:
    pc.start_job("x", "cd /tmp && ./x.sh 'a b'")
    [started] = [s.removeprefix("bash -lc ") for s in fake.scripts()]
    assert started.startswith("mkdir -p $HOME/.pc-jobs; rm -f $HOME/.pc-jobs/x.exit $HOME/.pc-jobs/x.pid; nohup ")
    wrapper = shlex.split(started.split("nohup ", 1)[1])
    assert wrapper[:2] == ["bash", "-c"] and wrapper[3:] == [">", "$HOME/.pc-jobs/x.log", "2>&1", "<", "/dev/null",
                                                             "&", "echo", "started"]  # fmt: skip
    session, rc = wrapper[2].rsplit("; ", 1)
    assert rc == "echo $? > $HOME/.pc-jobs/x.exit"  # outside the session: written however it ends
    assert shlex.split(session) == [
        "setsid", "-w", "bash", "-lc",
        "echo $$ $(cat /proc/sys/kernel/random/boot_id 2>/dev/null) > $HOME/.pc-jobs/x.pid; cd /tmp && ./x.sh 'a b'",
    ]  # fmt: skip
    assert subprocess.run(["bash", "-n", "-c", started], check=False).returncode == 0


def test_stop_job_stops_a_running_job_s_session_and_nothing_else(pc: Computer, fake: FakeApi) -> None:
    pc.stop_job("x", grace=3)
    [script] = [s.removeprefix("bash -lc ") for s in fake.scripts()]
    assert script.startswith("test -f $HOME/.pc-jobs/x.pid && ! test -f $HOME/.pc-jobs/x.exit || exit 0; ")
    assert '[ "$boot" = "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)" ] || exit 0' in script  # stale
    assert 'case "$s" in ""|*[!0-9]*|0|1) exit 0;; esac' in script
    assert 'pkill -TERM -s "$s" || exit 0; for _ in $(seq 6); do pgrep -s "$s"' in script
    assert script.index("pkill -TERM") < script.index("pkill -KILL") < script.index("test -f $HOME/.pc-jobs/x.exit &&")
    assert subprocess.run(["bash", "-n", "-c", script], check=False).returncode == 0


def test_a_job_here_writes_its_pid_and_exit_code_and_a_stop_of_none_does_nothing(here: Computer, tmp_path) -> None:
    pc = here
    assert pc.run_job("ok", "echo hi") == "hi\n"
    sid = (tmp_path / ".pc-jobs/ok.pid").read_text().split()[0]
    assert sid.isdigit() and sid != str(os.getsid(0))  # a session of its own
    with pytest.raises(JobFailed, match="exited 3"):
        pc.run_job("bad", "exit 3")
    pc.stop_job("ok")  # ended: nothing to stop
    pc.stop_job("never")  # never started
    assert pc.jobs() == ["bad", "ok"] and pc.jobs("b*") == ["bad"]


@pytest.mark.skipif(
    subprocess.run(["pgrep", "-s", "0"], capture_output=True, check=False).returncode != 0,
    reason="no process list here (a sandbox)",
)
def test_a_stopped_job_here_leaves_nothing_and_its_exit_code(here: Computer, tmp_path) -> None:
    pc = here
    # A process that leaves the job's process group, as `timeout` does: still in its session.
    pc.start_job("long", f"{sys.executable} -c 'import os, time; os.setpgid(0, 0); time.sleep(60)'; echo never")
    pid = tmp_path / ".pc-jobs/long.pid"
    for _ in range(50):
        if pid.is_file() and pc.sh(f"pgrep -s {pid.read_text().split()[0]} || true").strip():
            break
        time.sleep(0.1)
    pc.stop_job("long", grace=2)
    assert not pc.job_running("long") and (tmp_path / ".pc-jobs/long.exit").read_text().strip() == "143"
    assert not pc.sh(f"pgrep -s {pid.read_text().split()[0]} || true").strip()
    assert "never" not in (tmp_path / ".pc-jobs/long.log").read_text()
    with pytest.raises(JobFailed, match="exited 143"):
        pc.wait_job("long")


def test_waiting_for_a_job_waits_out_a_blip_but_not_a_computer_gone(pc: Computer, fake: FakeApi) -> None:
    for _ in range(BLIPS):
        fake.on_exec(".exit 2>/dev/null; echo", 255, once=True)
    fake.on_exec(".exit 2>/dev/null; echo", "0\ndone\n", once=True)
    fake.on_exec("tail -n 300", "done\n")
    assert pc.wait_job("x") == "done\n"
    fake.on_exec(".exit 2>/dev/null; echo", 255)
    with pytest.raises(NeviaError):
        pc.wait_job("x")
    assert sum(".exit 2>/dev/null; echo" in s for s in fake.scripts()) == 2 * BLIPS + 2


def test_sleep_puts_an_awake_computer_to_sleep_and_leaves_a_sleeping_one(pc: Computer, fake: FakeApi) -> None:
    state = lambda: (fake.find("computers", "pc") or {})["state"]  # noqa: E731
    pc.sleep()
    assert state() == "sleeping"
    pc.sleep()  # already asleep: nothing more
    pc.awake()
    assert state() != "sleeping"
