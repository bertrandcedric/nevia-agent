import subprocess

import pytest
from nevia_kit import NeviaError
from nevia_kit.testing import FakeApi

from acc.computer import Computer
from core.local.collect import Collect, _script


def test_download_reads_again_after_an_exec_without_a_result(monkeypatch) -> None:
    fake = FakeApi(validate=True)
    fake.add("computers", "acc-agent")
    nevia = fake.client()
    pc = Computer("acc-agent", nevia, 0)
    answers: list[object] = [KeyError("result"), b"jar"]

    def download(_ref: str, _path: str) -> bytes:
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, bytes)
        return answer

    monkeypatch.setattr(nevia.computers, "download", download)
    assert pc.download("app.jar") == b"jar"
    answers[:] = [KeyError("result")] * 3
    with pytest.raises(NeviaError, match="after 3 tries"):
        pc.download("app.jar")


def test_follow_job_shows_every_line_of_the_log_not_only_the_last() -> None:
    fake = FakeApi(validate=True)
    fake.add("computers", "m2p-agent")
    pc = Computer("m2p-agent", fake.client(), 0)
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
    pc.follow_job("m2p-setup", shown.append)
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


def test_an_upload_here_replaces_a_read_only_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_COMPUTER", "acc-agent")
    monkeypatch.setenv("HOME", str(tmp_path))
    pc = Computer("acc-agent", FakeApi(validate=True).client(), 0)
    guide = tmp_path / "loop" / "AGENTS.md"
    pc.upload(b"first", "loop/AGENTS.md", "444")
    pc.upload(b"second", "loop/AGENTS.md", "444")
    assert guide.read_bytes() == b"second"
    assert oct(guide.stat().st_mode & 0o777) == "0o444"
    assert [p.name for p in guide.parent.iterdir()] == ["AGENTS.md"]
