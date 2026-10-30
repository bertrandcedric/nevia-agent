import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import tarfile
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from nevia_kit.testing import FakeApi
from typer.testing import CliRunner

from core.activity import Activity, events
from core.agent import ai
from core.computer import JobFailed, bundled
from core.config import RUN_ID
from core.doomed import Doomed
from core.local import bootstrap
from core.nevia import CliError, Nevia
from core.results import REPORT, RESULT
from m2p import agent as loop
from m2p import follow, steps
from m2p import local as cli
from m2p.agent import Loop
from m2p.config import Settings
from m2p.follow import Try, _summary, _tries
from m2p.run import AGENT_JOB, Status, status_json, status_of
from tests.fake_nevia import FakeCli, exec_env


@pytest.fixture
def agent(nevia: Nevia, settings: Settings, fake: FakeApi, monkeypatch) -> Loop:
    fake.add("computers", settings.agent.computer)
    lp = Loop(settings, nevia, poll=0)
    monkeypatch.setattr(lp, "link", lambda: None)
    return lp


def test_run_passes_the_gateway_key_as_a_secret_template(agent: Loop, fake: FakeApi, monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Helsinki")  # your machine's, as `Bootstrap.remote` gives it
    agent.run()
    env = exec_env(fake, "remote step loop 3")  # the sessions' job
    assert env["TZ"] == "Europe/Helsinki"  # its logs in your time zone
    ai = agent.settings.ai
    assert env["ANTHROPIC_AUTH_TOKEN"] == f"{{{{secrets.M2P_{ai.gateway_secret}}}}}"  # Claude Code, the default
    assert env["ANTHROPIC_BASE_URL"] == ai.claude.gateway_url
    script = next(s for s in fake.scripts() if "remote step loop 3" in s)
    assert f"timeout {agent.settings.agent.hours}h" in script
    assert ai.gateway_secret not in fake.env[fake.get("computers", agent.name)["id"]]
    assert RUN_ID.fullmatch(fake.files[agent.name, f"{agent.dir}/RUN"].decode().strip())  # the run's id, as acc's


def test_run_with_copilot_passes_its_gateway(agent: Loop, fake: FakeApi) -> None:
    s = agent.settings
    agent.settings = s.model_copy(update={"ai": s.ai.model_copy(update={"engine": "copilot"})})
    agent.run()
    env = exec_env(fake, "remote step loop 3")
    assert env["COPILOT_PROVIDER_API_KEY"] == f"{{{{secrets.M2P_{s.ai.gateway_secret}}}}}"
    assert env["COPILOT_MODEL"] == s.ai.copilot.model and env["COPILOT_OFFLINE"] == "true"
    assert not any(k.startswith("ANTHROPIC_") for k in env)


def test_run_refuses_while_the_agent_runs(agent: Loop, fake: FakeApi) -> None:
    fake.on_exec("agent.exit && echo yes", "yes\n")
    with pytest.raises(RuntimeError, match="already running"):
        agent.run()


def test_tools_are_read_only_and_the_guide_names_the_resources(agent: Loop, fake: FakeApi) -> None:
    agent._upload_tools(agent.computer())
    d, s = agent.dir, agent.settings
    guide = fake.files[agent.name, f"{d}/AGENTS.md"].decode()
    assert s.postgres.name in guide
    assert s.mongo.computer in guide
    assert "a sample of 1% of each collection" in guide and "Never run `infer`" in guide  # the model is the pipeline's
    assert "{" not in guide  # every placeholder filled
    env = fake.files[agent.name, f"{d}/tools/env.sh"].decode()
    assert f"export M2P_PG={s.postgres.name}" in env
    assert "M2P_MONGO_DB" not in env
    assert not any("patches" in path for _, path in fake.files)


def test_status_reads_the_tries(agent: Loop, fake: FakeApi, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(steps, "HOME", tmp_path)
    fake.add("databases", "m2p-try2")
    d = tmp_path / agent.dir
    (d / "logs").mkdir(parents=True)
    (d / "ATTEMPTS.md").write_text("# Attempts\n---\n## Try 1: FAILED\n")
    (d / "logs/ai-1.log").write_text(
        "2026-10-28T20:41:58Z session 1 started\n"
        "junk before any event\n"
        "13:34:05 $ tools/branch.sh 1\n"
        "    # a new branch\n"  # under the event: part of it
        "13:34:06 says: done\n"
    )
    st = agent.status()
    assert st.branches == ["m2p-try2"]
    assert "Try 1: FAILED" in st.attempts
    assert st.sessions == "2026-10-28T20:41:58Z session 1 started"
    assert not st.done
    assert st.activity == [
        Activity("ai-1.log:3", "13:34:05", "$ tools/branch.sh 1\n    # a new branch"),
        Activity("ai-1.log:5", "13:34:06", "says: done"),
    ]
    (d / "DONE.md").write_text("done")
    assert agent.status().done
    assert not any("DONE.md" in s or "ATTEMPTS.md" in s for s in fake.scripts())  # its own files, read here


def test_status_takes_the_latest_session_s_end_with_the_ids_of_the_whole_log(
    agent: Loop, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(steps, "HOME", tmp_path)
    monkeypatch.setattr(loop, "ACTIVITY_LINES", 3)
    logs = tmp_path / agent.dir / "logs"
    logs.mkdir(parents=True)
    (logs / "ai-1.log").write_text("2026-10-28T20:00:00Z session 1 started\n13:00:00 says: old\n")
    os.utime(logs / "ai-1.log", (1, 1))  # the older session
    (logs / "ai-2.log").write_text(
        "2026-10-28T21:00:00Z session 2 started\n14:00:01 $ ls\n    a\n14:00:02 says: hi\n14:00:03 failed: boom\n"
    )
    st = agent.status()
    assert st.sessions.splitlines() == [
        "2026-10-28T20:00:00Z session 1 started",
        "2026-10-28T21:00:00Z session 2 started",
    ]
    # The last 3 lines: `    a` has lost its event, and the ids are the lines of the whole log.
    assert [(a.id, a.text, a.failed) for a in st.activity] == [
        ("ai-2.log:4", "says: hi", False),
        ("ai-2.log:5", "failed: boom", True),
    ]


def test_the_guide_names_the_mongo2pg_release(agent: Loop, fake: FakeApi) -> None:
    agent._upload_tools(agent.computer())
    assert any("rm -rf ~/loop/tools && mkdir -p ~/loop/tools" in c for c in fake.scripts())
    guide = fake.files[agent.name, f"{agent.dir}/AGENTS.md"].decode()
    assert f"upstream mongo2pg's release {agent.settings.mongo2pg.release}." in guide
    assert "build.sh" not in guide
    assert "Don't write to `logs/`" in guide
    assert "Don't use JSONB or JSON" in guide
    assert "the `hello` database, an app's customers: one collection, `customers`" in guide
    assert "bin/mongo2pg report -c tries/NAME/config/NAME.toml --post-import" in guide
    assert "don't read its source code" in guide
    assert "Don't verify a load again" in guide
    assert "# Done: `hello` in `m2p-pg`" in guide
    assert "Keep the SQL model mongo2pg inferred whenever you can, and fix the data instead" in guide
    assert "`tries/NAME/normalize.sql`" in guide and "Don't rewrite the export with Python" in guide
    assert "-c '\\c hello' -f tries/NAME/normalize.sql" in guide  # \\c after the URI, not -d, which replaces it
    assert "Such a table passes on its count alone" in guide
    assert "{" not in guide.replace("{{", "")  # every placeholder filled
    assert set(loop.TOOLS) == {"branch.sh"}
    assert all((agent.name, f"{agent.dir}/tools/{name}") in fake.files for name in ai.TOOLS)  # the renders, both


def _targz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_collect_brings_the_results_here_without_uris(agent: Loop, fake: FakeApi, tmp_path) -> None:
    archive = _targz({
        "ATTEMPTS.md": b"# Attempts\nloaded postgres://avnadmin:s3cret@host:5432/hello\n",
        "logs/ai-1.log": b"19:21:31 says: loaded\n",
    })  # fmt: skip
    stage = (agent.name, ".nevia-agent/collect.tar.gz")
    fake.on_exec("tar -czf", lambda argv, body: fake.files.update({stage: archive}) or "")
    dest = cli.collect(agent.settings, agent.nevia, tmp_path / "run")
    assert (dest / "ATTEMPTS.md").read_text() == "# Attempts\nloaded postgres://avnadmin:***@host:5432/hello\n"
    assert (dest / "logs" / "ai-1.log").exists()
    [pack] = [s for s in fake.scripts() if "tar -czf" in s]
    assert "format-patch" not in pack
    assert all(f"--exclude=./{shlex.quote(p)}" in pack for p in cli.COLLECT_SKIP)
    assert "agent-job.log" in pack and "skipped.txt" in pack
    assert any("rm -f ~/.nevia-agent/collect.tar.gz" in s for s in fake.scripts())


# Copilot's events as its session log holds them, with the noise around them.
_EVENTS = [
    {"type": "assistant.turn_start", "id": "e0", "timestamp": "2026-10-28T13:34:00.000Z", "data": {"turnId": "0"}},
    {"type": "tool.execution_start", "id": "e1", "timestamp": "2026-10-28T13:34:01.000Z",
     "data": {"toolName": "report_intent", "toolCallId": "c1", "arguments": {"intent": "Creating try 2"}}},
    {"type": "tool.execution_start", "id": "e2", "timestamp": "2026-10-28T13:34:02.000Z",
     "data": {"toolName": "bash", "toolCallId": "c2",
              "arguments": {"command": "psql postgres://u:pw@h/db\n  -c 'select 1'", "description": "check"}}},
    {"type": "tool.execution_complete", "id": "e3", "timestamp": "2026-10-28T13:34:03.000Z",
     "data": {"toolCallId": "c2", "success": False, "result": {"content": "x" * 10000}}},
    {"type": "tool.execution_start", "id": "e4", "timestamp": "2026-10-28T13:34:04.000Z",
     "data": {"toolName": "edit", "toolCallId": "c3", "arguments": {"path": "src/export.rs", "old_str": "a"}}},
    {"type": "assistant.message", "id": "e5", "timestamp": "2026-10-28T13:34:05.000Z",
     "data": {"content": "The column is too narrow.", "toolRequests": []}},
    {"type": "assistant.message", "id": "e6", "timestamp": "2026-10-28T13:34:06.000Z", "data": {"content": ""}},
    {"type": "session.error", "id": "e7", "timestamp": "2026-10-28T13:34:07.000Z", "data": {"message": "rate limited"}},
]  # fmt: skip


def _render(log: Path) -> str:
    """tools/render-copilot.jq over a session's raw output, as `ai.session` pipes it, its times in UTC."""
    core = Path(loop.__file__).parents[1] / "core" / "scripts"  # with common.jq, which it includes
    args = ["jq", "-nRr", "-L", str(core), "-f", str(core / "render-copilot.jq"), str(log)]
    env = os.environ | {"TZ": "UTC"}
    return subprocess.run(args, capture_output=True, text=True, check=True, env=env).stdout


@pytest.mark.skipif(not shutil.which("jq"), reason="jq runs on the agent computer")
def test_the_session_log_is_readable(tmp_path) -> None:
    raw = tmp_path / "raw"
    raw.write_text("plain text first\n" + "".join(json.dumps(e) + "\n" for e in _EVENTS))
    text = _render(raw)
    assert not any(line.startswith("{") for line in text.splitlines())  # no JSON left
    assert text.splitlines() == [
        "plain text first",
        "13:34:01 intent: Creating try 2",
        "13:34:02 $ psql postgres://u:pw@h/db",  # the collected copy and run's output scrub it
        "      -c 'select 1'",
        "    # check",
        "13:34:03 failed: " + "x" * 2000 + " …",
        "13:34:04 edit src/export.rs",
        "13:34:05 says: The column is too narrow.",
        "13:34:07 error: rate limited",
    ]


@pytest.mark.skipif(not shutil.which("jq"), reason="jq runs on the agent computer")
def test_activity_summarizes_what_the_agent_does(tmp_path) -> None:
    raw = tmp_path / "raw"
    raw.write_text("".join(json.dumps(e) + "\n" for e in _EVENTS))
    # What status takes from the session log: its events, with the lines under them.
    assert [(a.time, a.text, a.failed) for a in events(_render(raw).splitlines(), "ai-1.log")] == [
        ("13:34:01", "intent: Creating try 2", False),
        ("13:34:02", "$ psql postgres://u:***@h/db\n      -c 'select 1'\n    # check", False),
        ("13:34:03", "failed: " + "x" * 2000 + " …", True),
        ("13:34:04", "edit src/export.rs", False),
        ("13:34:05", "says: The column is too narrow.", False),
        ("13:34:07", "error: rate limited", False),
    ]


def test_run_reads_each_try_and_is_brief_by_default() -> None:
    attempts = (
        "# Attempts\n## Try 1: FAILED\n- What happened: import failed at line 439\n- Why: x\n"
        "- Change: orders.date as TEXT\n\n## Try 2: PASSED\n- What happened: all six tables match\n"
    )
    assert _tries(attempts) == [
        Try(1, "FAILED", "import failed at line 439", "orders.date as TEXT"),
        Try(2, "PASSED", "all six tables match", ""),
    ]
    assert _summary(_tries(attempts)) == "2 tries, the last PASSED"
    assert _summary(_tries("# Attempts\n")) == "no tries"
    acts = ("intent: Creating try 2", "$ ls", "edit x", "says: hi", "failed: boom", "error: rate limited")
    shown = [Activity("1", "t", t) for t in acts]
    assert [a.text for a in shown if a.brief] == [
        "intent: Creating try 2",
        "says: hi",
        "failed: boom",
        "error: rate limited",
    ]


def test_run_prints_a_timeline_in_local_time(monkeypatch) -> None:
    """The events' times are in your time zone already (the loop's job has your TZ); the session lines' are UTC."""
    states = iter([
        Status(True, False, "2026-10-28T20:41:58Z session 1 started", "# Attempts", [], {},
                    [Activity("s:1", "20:42:09", "says: starting on a fresh branch")]),
        Status(True, False, "2026-10-28T20:41:58Z session 1 started", "# Attempts\n## Try 1: FAILED\n"
                    "- What happened: import failed\n- Change: TEXT dates", ["m2p-try1"], {}, []),
        Status(False, True, "2026-10-28T20:41:58Z session 1 started\n2026-10-28T20:43:51Z session 1 ended with 0",
                    "# Attempts\n## Try 1: FAILED\n- What happened: import failed\n## Try 2: PASSED\n"
                    "- What happened: all match", [], {}, []),
    ])  # fmt: skip

    class Agent:
        def status(self) -> Status:
            return next(states)

        def computer(self, _poll: float) -> _Job:
            return _Job(polls=2, stopped=True)  # a stopped job fails: the status says how it ended

    lines, times = [], []
    monkeypatch.setattr(follow, "_event", lambda when, text, style=None: lines.append(text) or times.append(when))
    follow.follow(cast(cli.Local, Agent()), 0)
    session = datetime(2026, 10, 28, 20, 41, 58, tzinfo=UTC).astimezone().strftime("%H:%M:%S")
    assert times[:2] == [session, "20:42:09"]  # the event's time as it came
    assert lines == [
        "session 1 started",
        "agent: starting on a fresh branch",
        "branch m2p-try1 created",
        "try 1 FAILED: import failed",
        "  next: TEXT dates",
        "session 1 ended with 0",
        "branch m2p-try1 deleted",
        "try 2 PASSED: all match",
        "agent done: 2 tries, the last PASSED",
    ]


class _Job:
    """The agent computer as `_follow` waits on it: `polls` polls, then the job's end."""

    def __init__(self, polls: int = 1, *, stopped: bool = False) -> None:
        self.polls, self.stopped = polls, stopped

    def wait_job(self, name: str, *, on_poll) -> str:
        assert name == AGENT_JOB
        for _ in range(self.polls):
            on_poll()
        if self.stopped:
            raise JobFailed(f"{name} on m2p-agent exited 143:")
        return ""


class _Agent:
    """A Loop for the CLI: running or not, and what the CLI asked of it."""

    def __init__(self, running: bool, tmp_path: Path) -> None:
        self.name, self.calls, self._running, self.tmp = "m2p-agent", [], running, tmp_path

    def running(self) -> bool:
        return self._running

    def run(self) -> None:
        self.calls.append("run")

    def status(self) -> Status:
        self.calls.append("status")
        return Status(False, True, "", "", [], {})

    def computer(self, _poll: float) -> _Job:
        return _Job()

    def collect(self) -> Path:
        self.calls.append("collect")
        (self.tmp / "logs").mkdir(exist_ok=True)
        return self.tmp

    @contextmanager
    def asleep_after(self, job: str) -> Generator[None]:
        try:
            yield
        finally:
            self.calls.append("sleep")


@pytest.mark.parametrize(
    ("running", "calls"),
    [(False, ["run", "status", "status", "collect", "sleep"]), (True, ["status", "status", "collect", "sleep"])],
)
def test_run_follows_the_agent_then_collects_and_puts_it_to_sleep(
    monkeypatch, tmp_path, running: bool, calls: list[str]
) -> None:
    agent = _Agent(running, tmp_path)
    monkeypatch.setattr(cli, "_agent", lambda: agent)
    result = CliRunner().invoke(cli.app, ["run", "--interval", "0"])
    assert result.exit_code == 0, result.output
    assert agent.calls == calls


def test_clean_leaves_the_setup_and_deletes_the_branches(agent: Loop, fake: FakeApi, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(steps, "HOME", tmp_path)
    ran: list[list[str]] = []
    monkeypatch.setattr(steps, "run", lambda args, **_k: ran.append(list(args)) or "")  # no git here
    d_ = tmp_path / agent.dir
    for keep in ("tools/env.sh", "bin/mongo2pg", "mongo2pg/README.md"):
        (d_ / keep).parent.mkdir(parents=True, exist_ok=True)
        (d_ / keep).write_text("x")
    (d_ / "AGENTS.md").write_text("guide")
    (d_ / "tries/1").mkdir(parents=True)
    (d_ / "tries/1/out.csv").write_text("x")
    (d_ / "DONE.md").write_text("done")
    (d_ / "ATTEMPTS.md").write_text("# Attempts\n## Try 1: PASSED\n")
    (d_ / "logs").symlink_to(tmp_path)  # a link goes, not what it points to
    jobs = tmp_path / ".m2p-jobs"
    jobs.mkdir()
    for f in ("agent.log", "agent.exit", "seed.log"):
        (jobs / f).write_text("x")
    fake.add("databases", "m2p-try1")
    fake.add("databases", "m2p-try2")
    fake.add("databases", "m2p-pg-try")  # not a try's: another prefix
    s = agent.settings
    fake.add("computers", s.seed_computer)
    fake.add("databases", s.loaded, protected=True)  # a passing run's
    agent.nevia.databases.protect(s.postgres.name)
    d = agent.doomed_clean()
    assert d.databases == ["m2p-try1", "m2p-try2"] and d.dirs == [agent.dir]
    agent.clean(d)
    assert fake.find("databases", "m2p-try1") is None and fake.find("databases", "m2p-pg-try") is not None
    assert fake.find("databases", "m2p-pg") is not None and fake.find("databases", s.loaded) is not None
    assert fake.find("computers", s.seed_computer) is not None
    assert sorted(p.name for p in d_.iterdir()) == sorted([*loop.CLEAN_KEEP, "ATTEMPTS.md", "logs", "tries"])
    assert (d_ / "tools/env.sh").exists() and not any((d_ / "tries").iterdir()) and not (d_ / "logs").is_symlink()
    assert (d_ / "ATTEMPTS.md").read_text() == "# Attempts\n"
    assert sorted(p.name for p in jobs.iterdir()) == ["seed.log"]
    src = str(d_ / "mongo2pg")
    assert ran == [
        ["git", "-C", src, "switch", "-q", "-f", "-C", "agent", agent.settings.mongo2pg.release],
        ["git", "-C", src, "clean", "-q", "-fdx"],
    ]
    assert any("DROP SCHEMA IF EXISTS hello CASCADE" in s and "'\\c hello'" in s for s in fake.scripts())


def test_clean_refuses_while_the_agent_runs(agent: Loop, fake: FakeApi) -> None:
    fake.on_exec("agent.exit && echo yes", "yes\n")
    with pytest.raises(RuntimeError, match="running"):
        agent.doomed_clean()


def test_clean_lists_the_branches_and_the_files_then_asks(agent: Loop, fake: FakeApi, monkeypatch) -> None:
    deleted: list[str] = []
    fake.on_exec("remote clean --yes", lambda _a, _b: deleted.append("clean") or "deleted database m2p-try1\n")
    fake.on_exec("remote clean", Doomed(databases=["m2p-try1"], dirs=[agent.dir]).to_json() + "\n")
    shown: list[str] = []
    monkeypatch.setattr(bootstrap, "show", shown.append)
    asked: list[str] = []
    monkeypatch.setattr(
        bootstrap.typer, "confirm", lambda q, **_k: asked.append(q) or (_ for _ in ()).throw(SystemExit(1))
    )
    lp = cli.Local(agent.settings, agent.nevia)
    with pytest.raises(SystemExit):
        lp.clean(yes=False)
    assert shown[0] == f"databases: m2p-try1\nfiles: ~/{agent.dir}" and "schema hello" in asked[0]
    assert not deleted  # nothing before the yes
    lp.clean(yes=True)
    assert deleted == ["clean"]


def test_branch_sh_takes_the_prefix_from_env_sh(agent: Loop) -> None:
    assert f"export M2P_TRIES={agent.settings.tries}\n" in agent._env_sh().decode()
    assert agent.settings.tries == f"{agent.settings.prefix}try"
    script = bundled("m2p", "scripts/branch.sh").decode()
    assert 'name="$M2P_TRIES$n"' in script and "m2p-try$n" not in script


def test_run_sends_this_repositorys_tools_first(agent: Loop, fake: FakeApi) -> None:
    fake.files[agent.name, f"{agent.dir}/tools/branch.sh"] = b"an older branch.sh"
    agent.run()
    for name in loop.TOOLS:
        assert fake.files[agent.name, f"{agent.dir}/tools/{name}"] == bundled("m2p", f"scripts/{name}")
    guide = fake.files[agent.name, f"{agent.dir}/AGENTS.md"].decode()
    assert "This guide is your prompt" in guide and "ATTEMPTS.md" in guide  # each session's prompt
    scripts = fake.scripts()
    replaced = next(i for i, s in enumerate(scripts) if "rm -rf ~/loop/tools" in s)
    started = next(i for i, s in enumerate(scripts) if "nohup" in s and "remote step loop" in s)
    assert replaced < started


def test_the_sessions_refuse_to_start_without_their_renderer(agent: Loop, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(steps, "HOME", tmp_path)
    (tmp_path / agent.dir / "tools").mkdir(parents=True)
    with pytest.raises(RuntimeError, match=r"tools/render-claude\.jq is missing"):
        steps.loop(agent.settings, 1)


def test_each_session_keeps_its_result_and_the_run_its_total(agent: Loop, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(steps, "HOME", tmp_path)
    monkeypatch.setattr(steps, "model", lambda s: tmp_path)  # tested on its own below
    d = tmp_path / agent.dir
    for f in (f"tools/{ai.render(agent.settings.ai)}", "tools/common.jq", "AGENTS.md"):
        (d / f).parent.mkdir(parents=True, exist_ok=True)
        (d / f).write_text("")
    (d / "RUN").write_text("261029170000\n")
    end = {"type": "result", "subtype": "success", "num_turns": 3, "total_cost_usd": 1.234, "duration_ms": 2000,
           "modelUsage": {"m": {"inputTokens": 5, "costUSD": 1.234}}}  # fmt: skip

    def session(cmd, *, cwd, env, render, log) -> int:
        assert env["SHELL"] == "/bin/bash"  # the AI's commands in bash, whatever the user's login shell
        log.with_suffix(".jsonl").write_text(f"junk\n{json.dumps(end)}\n")
        if log.name == "ai-2.log":
            (d / "DONE.md").write_text("done")
        return 0

    monkeypatch.setattr(ai, "session", session)
    assert steps.loop(agent.settings, 3) == "done, Claude Code $2.47"
    assert json.loads((d / "logs/ai-1.json").read_text()) == end
    ended = (d / "logs/ai-2.log").read_text().splitlines()[-1]
    assert ended.endswith(" session 2 ended with 0, $1.23")
    assert loop.SESSION_LINE.match(ended)
    total = json.loads((d / RESULT).read_text())
    assert (total["sessions"], total["num_turns"], total["total_cost_usd"]) == (2, 6, 2.468)
    assert total["modelUsage"]["m"]["inputTokens"] == 10
    report = (d / REPORT).read_text()
    assert report.startswith("# Run 261029170000\n") and "| 2 | 0 | 3 | 2 s | $1.2340 |" in report
    assert "- cost: **$2.4680**" in report


def test_stop_stops_the_agent_s_job_not_command_lines(agent: Loop, fake: FakeApi) -> None:
    """The loop's job is stopped as a whole (`Computer.stop_job`), whatever its command line; what could have left
    its session goes by the engine's binary."""
    agent.stop()
    script = "\n".join(fake.scripts())
    assert "read -r s boot < $HOME/.m2p-jobs/agent.pid" in script and 'pkill -TERM -s "$s"' in script
    assert "pkill -f" not in script
    assert f"pkill -x {ai.binary(agent.settings.ai)} || true" in script


def test_stop_ends_the_run_when_it_stops_the_job(agent: Loop, fake: FakeApi, monkeypatch) -> None:
    """Should the job have been killed before its own end was over (`remote_step`)."""
    ended: list[int] = []
    monkeypatch.setattr(agent, "ended", lambda: ended.append(1))
    agent.stop()
    assert not ended  # nothing ran
    fake.on_exec("agent.log && ! test -f", "yes\n")
    agent.stop()
    assert ended == [1]


def _secret(fake: FakeApi, name: str) -> str:
    """The latest value written to the workspace secret `name` (FakeApi keeps none)."""
    refs = {name, fake.get("secrets", name)["id"]}
    writes = [c for c in fake.calls if c.op in ("createSecret", "updateSecret")]
    return next(c.body["value"] for c in reversed(writes) if refs & {c.path.rsplit("/", 1)[-1], c.body.get("name")})


def _resets(nevia: Nevia) -> list[str]:
    return [a[-1] for a in cast("FakeCli", nevia.run).calls if a[:2] == ["pg", "reset-password"]]


def test_a_run_s_end_resets_the_passwords_the_ai_saw(agent: Loop, fake: FakeApi, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(steps, "HOME", tmp_path)
    s, nevia = agent.settings, agent.nevia
    for name in ("m2p-try1", "m2p-try2", "m2p-pg-try"):
        fake.add("databases", name)
    secret = s.secret(s.postgres.secret)
    nevia.databases.store_uri(s.postgres.name, secret)
    old = _secret(fake, secret)
    tries = tmp_path / agent.dir / "tries"
    (tries / "m2p-try1").mkdir(parents=True)
    (tries / "m2p-try1.uri").write_text("postgres://app:secret@m2p-try1.db.example:5432/app\n")
    (tries / "m2p-try1" / "config.toml").write_text("the project")
    agent.ended()
    assert _resets(nevia) == ["m2p-try1", "m2p-try2", s.postgres.name]  # not another prefix's
    new = _secret(fake, secret)
    assert new != old and ":secret1@" in new  # the target's URI as of its reset
    assert not (tries / "m2p-try1.uri").exists() and (tries / "m2p-try1" / "config.toml").exists()
    assert not fake.get("databases", s.postgres.name)["protected"]  # no DONE.md: not a load to keep
    assert fake.find("databases", s.loaded) is None
    printed = capsys.readouterr()
    assert "secret1" not in printed.out + printed.err


def test_a_run_that_passed_protects_the_target_and_keeps_a_copy(
    agent: Loop, fake: FakeApi, nevia: Nevia, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(steps, "HOME", tmp_path)
    s = agent.settings
    d = tmp_path / agent.dir
    d.mkdir()
    (d / "DONE.md").write_text("done")
    (d / "RUN").write_text("261030120000\n")
    older = fake.add("databases", s.loaded, protected=True)["id"]  # an earlier run's copy
    agent.ended()
    pg, copy = fake.get("databases", s.postgres.name), fake.get("databases", s.loaded)
    assert pg["protected"] is True
    assert copy["id"] != older and copy["protected"] is True and copy["parent"] == pg["id"]
    assert s.loaded == f"{s.postgres.name}-loaded" and not s.is_try(s.loaded) and agent._branches() == []
    agent.ended()  # again, as `stop` may: the copy is this run's already
    assert fake.get("databases", s.loaded)["id"] == copy["id"]
    with pytest.raises(CliError, match="exited 5"):
        nevia.databases.delete(s.postgres.name)


def test_the_loop_job_ends_the_run_however_the_sessions_end(monkeypatch, capsys) -> None:
    ended: list[int] = []
    monkeypatch.setattr(loop, "_here", lambda: SimpleNamespace(ended=lambda: ended.append(1)))
    term = signal.getsignal(signal.SIGTERM)
    try:
        monkeypatch.setattr(steps, "loop", lambda _s, _n: "done")
        loop.remote_step("loop", "3")
        assert ended == [1] and capsys.readouterr().out.splitlines()[-1] == "done"  # the job's last line
        monkeypatch.setattr(steps, "loop", lambda _s, _n: os.kill(os.getpid(), signal.SIGTERM))  # stop, timeout
        with pytest.raises(SystemExit) as stopped:
            loop.remote_step("loop", "3")
        assert stopped.value.code == 128 + signal.SIGTERM and ended == [1, 1]
    finally:
        signal.signal(signal.SIGTERM, term)


def test_the_status_goes_by_field_name() -> None:
    st = Status(True, False, "s", "# Attempts", ["m2p-try1"], {"avail": "1MB"}, [Activity("a:1", "t", "x")])
    assert status_of(status_json(st)) == st
    sent = json.loads(status_json(st))
    assert status_of(json.dumps(dict(reversed(sent.items())))) == st  # not by position
    del sent["activity"]
    assert status_of(json.dumps(sent)).activity == []


def test_running_asks_the_agent_computer_for_the_job(nevia: Nevia, settings: Settings, fake: FakeApi) -> None:
    fake.add("computers", settings.agent.computer)
    fake.on_exec("agent.exit && echo yes", "yes\n")
    assert cli.Local(settings, nevia).running()
    assert not any("remote" in s for s in fake.scripts())  # no round trip through this code there


def test_status_prints_the_tries_and_the_latest_actions(monkeypatch) -> None:
    class Agent:
        def status(self) -> Status:
            acts = [Activity(f"s:{n}", "13:34:0" + str(n), f"$ step {n}") for n in range(5)]
            return Status(True, False, "session 1 started", "## Try 1: FAILED", ["m2p-try1"], {}, acts)

        def show_operations(self) -> None:
            print("running on Nevia: computer.branch")  # noqa: T201 - what frame.Local prints

    monkeypatch.setattr(cli, "_agent", Agent)
    result = CliRunner().invoke(cli.app, ["status", "--last", "2"])
    assert result.exit_code == 0, result.output
    assert "m2p-try1" in result.output and "Try 1: FAILED" in result.output
    assert result.output.index("Try 1") < result.output.index("computer.branch") < result.output.index("step 3")
    assert "$ step 4" in result.output and "$ step 3" in result.output and "$ step 2" not in result.output


def test_destroy_deletes_what_the_agent_made_and_nothing_else(agent: Loop, fake: FakeApi, nevia: Nevia) -> None:
    s = agent.settings
    fake.add("computers", s.mongo.computer)
    fake.add("computers", s.seed_computer)
    fake.add("computers", "someone-elses")
    fake.add("databases", "m2p-try3")
    fake.add("databases", "m2p-try3-x")  # not a try's
    fake.add("databases", s.loaded, protected=True)
    fake.add("databases", "other-db")
    nevia.databases.protect(s.postgres.name)
    for name in (s.secret(s.postgres.secret), s.secret(s.ai.gateway_secret), s.postgres.secret, "OTHER"):
        nevia.secrets.set(name, "x")
    doomed = agent.doomed()
    assert doomed.computers == [s.mongo.computer, s.seed_computer]  # the agent computer is your machine's to delete
    assert doomed.databases == ["m2p-try3", s.loaded, s.postgres.name]  # the branches before their database
    assert doomed.unprotected == [s.loaded, s.postgres.name]
    assert f"protection cleared first: {s.loaded}, {s.postgres.name}" in doomed.lines()
    assert doomed.secrets == [s.secret(s.postgres.secret)]  # the gateway key's too
    agent.destroy(doomed)
    assert not agent.doomed()
    assert fake.find("computers", "someone-elses") is not None
    assert fake.find("computers", s.agent.computer) is not None
    assert fake.find("databases", "other-db") is not None and fake.find("databases", "m2p-try3-x") is not None
    left = sorted(sec["name"] for sec in nevia.secrets.list())
    assert left == sorted([s.secret(s.ai.gateway_secret), s.postgres.secret, "OTHER"])


def test_status_lists_the_branches_once_in_a_while(agent: Loop, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(steps, "HOME", tmp_path)
    listed: list[int] = []
    monkeypatch.setattr(agent, "_branches", lambda dbs=None: listed.append(1) or ["m2p-try1"])
    assert agent._recent_branches() == ["m2p-try1"]
    assert agent._recent_branches() == ["m2p-try1"]
    assert len(listed) == 1  # the second within BRANCHES_FOR: from the cache, no sign-in


_FAKE_MONGO2PG = """#!/bin/sh
# init: the config mongo2pg writes, `number = 1000` among it; infer: the model, and the sample it read.
case "$1" in
  init) mkdir -p "$3/$5/config"; printf '[source]\\nnumber = 1000\\n# percent = 10.0\\n' > "$3/$5/config/$5.toml";;
  infer) d=$(dirname "$(dirname "$3")"); mkdir -p "$d/schema/tables" "$d/source"
         grep '^percent' "$3" > "$d/schema/sample";;
esac
"""


def _model_ready(agent: Loop, tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(steps, "HOME", tmp_path)
    binary = tmp_path / agent.dir / "bin/mongo2pg"
    binary.parent.mkdir(parents=True)
    binary.write_text(_FAKE_MONGO2PG)
    binary.chmod(0o755)
    (tmp_path / agent.dir / "tries").mkdir()
    return tmp_path / agent.dir / "tries"


def test_the_model_is_inferred_once_with_the_sample_set_and_kept_read_only(agent: Loop, tmp_path, monkeypatch) -> None:
    tries = _model_ready(agent, tmp_path, monkeypatch)
    project = steps.model(agent.settings)
    assert project == tries / "model"
    conf = (project / "config/model.toml").read_text()
    assert "percent = 1\n" in conf and "number" not in conf  # only `number` became the percent
    assert (project / "schema/sample").read_text() == "percent = 1\n"  # infer read it
    assert not os.access(project / "config/model.toml", os.W_OK)  # a try copies it, never changes it
    (project / "schema/sample").chmod(0o644)  # would a second call infer again? it must not
    (project / "schema/sample").write_text("untouched")
    steps.model(agent.settings)
    assert (project / "schema/sample").read_text() == "untouched"


def test_clean_deletes_the_read_only_model(agent: Loop, tmp_path, monkeypatch) -> None:
    tries = _model_ready(agent, tmp_path, monkeypatch)
    steps.model(agent.settings)
    monkeypatch.setattr(steps, "run", lambda *a, **k: "")  # the checkout's git
    steps.reset(agent.settings, ("bin",))
    assert not (tries / "model").exists()
