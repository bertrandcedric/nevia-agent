import pytest
from nevia_kit import Nevia
from nevia_kit.testing import FakeApi

from core.computer import bundled
from m2p.computer import Computer, JobFailed, redact
from m2p.config import Settings


@pytest.fixture
def pc(nevia: Nevia, settings: Settings) -> Computer:
    return Computer("m2p-demo", nevia, 0, settings=settings)


def test_redact_hides_uri_passwords() -> None:
    line = "connecting to postgres://avnadmin:s3cr3t@host:5432/db?sslmode=require"
    assert redact(line) == "connecting to postgres://avnadmin:***@host:5432/db?sslmode=require"


def test_bundled_scripts_are_packaged() -> None:
    for name in ["render.jq", "branch.sh"]:
        assert bundled("m2p", f"scripts/{name}")
    for name in ["lib.sh", "mongo-host.sh", "mongo-up.sh", "common.jq", "setup-runtime.sh"]:  # both agents'
        assert bundled("core", f"scripts/{name}")


def test_job_waits_for_the_exit_file(pc: Computer, fake: FakeApi) -> None:
    fake.on_exec("cat $HOME/.m2p-jobs/export.exit", "\nexporting\n", once=True)
    fake.on_exec("cat $HOME/.m2p-jobs/export.exit", "0\ndone\n")
    fake.on_exec("tail -n 300 $HOME/.m2p-jobs/export.log", "line 1\ndone\n")
    assert pc.run_job("export", "./export.sh").endswith("done\n")
    started = fake.scripts()[0]
    assert "nohup" in started
    assert "echo $? > $HOME/.m2p-jobs/export.exit" in started


def test_failed_job_raises_with_the_log_tail(pc: Computer, fake: FakeApi) -> None:
    fake.on_exec("cat $HOME/.m2p-jobs/import.exit", "1\nError: boom\n")
    fake.on_exec("tail -n 300 $HOME/.m2p-jobs/import.log", "postgres://u:pw@h/db\nError: boom\n")
    with pytest.raises(JobFailed, match="exited 1") as err:
        pc.run_job("import", "./import.sh")
    assert "pw" not in str(err.value)


def test_psql_switches_database_and_keeps_the_uri_remote(pc: Computer, fake: FakeApi) -> None:
    pc.psql("SELECT 1", database="hello")
    cmd = fake.scripts()[0]
    assert 'psql "$DATABASE_URL"' in cmd
    assert "'\\c hello'" in cmd


def test_psql_gets_the_targets_uri_per_command_even_on_the_agent_computer(
    pc: Computer, fake: FakeApi, monkeypatch
) -> None:
    monkeypatch.setenv("AGENT_COMPUTER", pc.name)  # here: still Nevia's exec, for the secret's value
    pc.psql("SELECT 1")
    [(_, _, body)] = [e for e in fake.execs if "psql" in " ".join(e[1])]
    assert body["env"] == {"DATABASE_URL": "{{secrets.M2P_PG_URL}}"}
