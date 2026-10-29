import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from nevia_kit.testing import FakeApi
from typer.testing import CliRunner

from core.agent import ai
from core.computer import bundled
from core.nevia import Nevia
from m2p import agent as loop
from m2p import local as cli
from m2p import steps
from m2p.agent import Loop
from m2p.config import Settings
from m2p.local import Try, _brief, _summary, _tries


@pytest.fixture
def agent(nevia: Nevia, settings: Settings, fake: FakeApi, monkeypatch) -> Loop:
    fake.add("computers", settings.agent.computer)
    lp = Loop(settings, nevia, poll=0)
    monkeypatch.setattr(lp, "link", lambda: None)
    return lp


def _exec_env(fake: FakeApi, needle: str) -> dict[str, str]:
    return next(body.get("env") or {} for _, argv, body in fake.execs if needle in " ".join(argv))


def test_run_passes_the_gateway_key_as_a_secret_template(agent: Loop, fake: FakeApi) -> None:
    agent.run()
    env = _exec_env(fake, "remote step loop 3")  # the sessions' job
    ai = agent.settings.ai
    assert env["ANTHROPIC_AUTH_TOKEN"] == f"{{{{secrets.M2P_{ai.gateway_secret}}}}}"  # Claude Code, the default
    assert env["ANTHROPIC_BASE_URL"] == ai.claude.gateway_url
    script = next(s for s in fake.scripts() if "remote step loop 3" in s)
    assert f"timeout {agent.settings.agent.hours}h" in script
    assert ai.gateway_secret not in fake.env[fake.get("computers", agent.name)["id"]]


def test_run_with_copilot_passes_its_gateway(agent: Loop, fake: FakeApi) -> None:
    s = agent.settings
    agent.settings = s.model_copy(update={"ai": s.ai.model_copy(update={"engine": "copilot"})})
    agent.run()
    env = _exec_env(fake, "remote step loop 3")
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
    assert "{" not in guide  # every placeholder filled
    env = fake.files[agent.name, f"{d}/tools/env.sh"].decode()
    assert f"export M2P_PG={s.postgres.name}" in env
    assert "M2P_MONGO_DB" not in env
    assert not any("patches" in path for _, path in fake.files)


def test_status_reads_the_tries(agent: Loop, fake: FakeApi) -> None:
    fake.add("databases", "m2p-try2")
    fake.on_exec(
        "DONE.md",
        "done=0\n@@m2p@@\nsession 1 started\n@@m2p@@\n"
        "session-1.log:3:13:34:05 $ tools/branch.sh 1\n"
        "@@m2p@@\n# Attempts\n---\n## Try 1: FAILED\n",
    )
    st = agent.status()
    assert st.branches == ["m2p-try2"]
    assert "Try 1: FAILED" in st.attempts
    assert st.sessions == "session 1 started"
    assert not st.done
    assert st.activity == [loop.Activity("session-1.log:3", "13:34:05", "$ tools/branch.sh 1")]


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
        "logs/session-1.log": b"19:21:31 says: loaded\n",
    })  # fmt: skip
    stage = (agent.name, ".nevia-agent/collect.tar.gz")
    fake.on_exec("tar -czf", lambda argv, body: fake.files.update({stage: archive}) or "")
    dest = cli.collect(agent.settings, agent.nevia, tmp_path / "run")
    assert (dest / "ATTEMPTS.md").read_text() == "# Attempts\nloaded postgres://avnadmin:***@host:5432/hello\n"
    assert (dest / "logs" / "session-1.log").exists()
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
    # What status takes from the session log: its events' first lines, numbered.
    lines = [f"session-1.log:{n}:{line}" for n, line in enumerate(_render(raw).splitlines(), 1) if line[:2].isdigit()]
    assert [(a.time, a.text, a.failed) for a in loop.activity("\n".join(lines))] == [
        ("13:34:01", "intent: Creating try 2", False),
        ("13:34:02", "$ psql postgres://u:***@h/db", False),
        ("13:34:03", "failed: " + "x" * 292, True),
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
    shown = [loop.Activity("1", "t", t) for t in acts]
    assert [a.text for a in shown if _brief(a)] == [
        "intent: Creating try 2",
        "says: hi",
        "failed: boom",
        "error: rate limited",
    ]


def test_run_prints_a_timeline_in_local_time(monkeypatch) -> None:
    states = iter([
        loop.Status(True, False, "2026-10-28T20:41:58Z session 1 started", "# Attempts", [], {},
                    [loop.Activity("s:1", "20:42:09", "says: starting on a fresh branch")]),
        loop.Status(True, False, "2026-10-28T20:41:58Z session 1 started", "# Attempts\n## Try 1: FAILED\n"
                    "- What happened: import failed\n- Change: TEXT dates", ["m2p-try1"], {}, []),
        loop.Status(False, True, "2026-10-28T20:41:58Z session 1 started\n2026-10-28T20:43:51Z session 1 ended with 0",
                    "# Attempts\n## Try 1: FAILED\n- What happened: import failed\n## Try 2: PASSED\n"
                    "- What happened: all match", [], {}, []),
    ])  # fmt: skip

    class Agent:
        def status(self) -> loop.Status:
            return next(states)

    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    lines = []
    monkeypatch.setattr(cli, "_event", lambda when, text, style=None: lines.append(text))
    cli._follow(cast(cli.Local, Agent()), 0)
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
    assert cli._local("2026-10-28T20:41:58Z") == datetime(2026, 10, 28, 20, 41, 58, tzinfo=UTC).astimezone().strftime(
        "%H:%M:%S"
    )


class _Agent:
    """A Loop for the CLI: running or not, and what the CLI asked of it."""

    def __init__(self, running: bool, tmp_path: Path) -> None:
        self.name, self.calls, self._running, self.tmp = "m2p-agent", [], running, tmp_path

    def running(self) -> bool:
        return self._running

    def run(self) -> None:
        self.calls.append("run")

    def status(self) -> loop.Status:
        self.calls.append("status")
        return loop.Status(False, True, "", "", [], {})

    def collect(self, dest: Path | None = None) -> Path:
        self.calls.append("collect")
        (self.tmp / "logs").mkdir(exist_ok=True)
        return self.tmp


@pytest.mark.parametrize(("running", "calls"), [(False, ["run", "status", "collect"]), (True, ["status", "collect"])])
def test_run_follows_the_agent_then_collects(monkeypatch, tmp_path, running: bool, calls: list[str]) -> None:
    agent = _Agent(running, tmp_path)
    monkeypatch.setattr(cli, "_agent", lambda: agent)
    result = CliRunner().invoke(cli.app, ["run", "--interval", "0"])
    assert result.exit_code == 0, result.output
    assert agent.calls == calls


def test_clean_leaves_the_setup_and_deletes_the_branches(agent: Loop, fake: FakeApi) -> None:
    fake.add("databases", "m2p-try1")
    fake.add("databases", "m2p-try2")
    assert agent.clean() == ["m2p-try1", "m2p-try2"]
    assert fake.find("databases", "m2p-try1") is None
    assert fake.find("databases", "m2p-pg") is not None
    script = next(s for s in fake.scripts() if "find . -mindepth 1" in s)
    assert all(f"! -name {k}" in script for k in loop.CLEAN_KEEP)
    assert f"switch -q -f -C agent {agent.settings.mongo2pg.release}" in script
    assert any("DROP SCHEMA IF EXISTS hello CASCADE" in s and "'\\c hello'" in s for s in fake.scripts())


def test_clean_refuses_while_the_agent_runs(agent: Loop, fake: FakeApi) -> None:
    fake.on_exec("agent.exit && echo yes", "yes\n")
    with pytest.raises(RuntimeError, match="running"):
        agent.clean()
    assert not any("find . -mindepth 1" in s for s in fake.scripts())


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


def test_stop_does_not_kill_itself(agent: Loop, fake: FakeApi) -> None:
    """pkill -f matches whole command lines: a pattern that matches the shell running it kills it (143)."""
    agent.stop()
    script = next(s for s in fake.scripts() if "pkill" in s)
    patterns = re.findall(r"pkill -f '([^']+)'", script)
    assert patterns
    assert not any(re.search(p, script) for p in patterns)
    assert re.search(patterns[0], "timeout 6.0h /home/nevia/.nevia-agent/bin/agent m2p remote step loop 3")
    assert re.search(patterns[1], "/home/nevia/.nevia-agent/venv/bin/python -m core m2p remote step loop 3")
    wrapper = ("bash -lc timeout 6.0h ~/.nevia-agent/bin/agent m2p remote step loop 3; "
               "echo $? > /home/nevia/.m2p-jobs/agent.exit")  # fmt: skip
    assert not any(re.search(p, wrapper) for p in patterns)  # it still writes the exit code


def test_status_prints_the_tries_and_the_latest_actions(monkeypatch) -> None:
    class Agent:
        def status(self) -> loop.Status:
            acts = [loop.Activity(f"s:{n}", "13:34:0" + str(n), f"$ step {n}") for n in range(5)]
            return loop.Status(True, False, "session 1 started", "## Try 1: FAILED", ["m2p-try1"], {}, acts)

    monkeypatch.setattr(cli, "_agent", Agent)
    result = CliRunner().invoke(cli.app, ["status", "--last", "2"])
    assert result.exit_code == 0, result.output
    assert "m2p-try1" in result.output and "Try 1: FAILED" in result.output
    assert "$ step 4" in result.output and "$ step 3" in result.output and "$ step 2" not in result.output


def _key_principal(fake: FakeApi, nevia: Nevia, name: str, description: str) -> str:
    """A private-key principal as the real API keeps it; the fake keeps its example's auth and name."""
    pid = nevia.kit.access.add_key_principal("editor", name=name, description=description)["access"]["principal"]["id"]
    fake.access[pid]["principal"].update(auth="private_key", name=name)
    return pid


def test_destroy_deletes_what_the_agent_made_and_nothing_else(agent: Loop, fake: FakeApi, nevia: Nevia) -> None:
    s = agent.settings
    fake.add("computers", s.mongo.computer)
    fake.add("computers", "someone-elses")
    fake.add("databases", "m2p-try3")
    fake.add("databases", "other-db")
    for name in (s.secret(s.postgres.secret), s.secret(s.ai.gateway_secret), s.postgres.secret, "OTHER"):
        nevia.secrets.set(name, "x")
    doomed = agent.doomed()
    assert doomed.computers == [s.mongo.computer]  # the agent computer is your machine's to delete
    assert doomed.databases == ["m2p-try3", s.postgres.name]  # the branch before its database
    assert doomed.secrets == [s.secret(s.postgres.secret)]  # the gateway key's too
    agent.destroy(doomed)
    assert not agent.doomed()
    assert fake.find("computers", "someone-elses") is not None
    assert fake.find("computers", s.agent.computer) is not None
    assert fake.find("databases", "other-db") is not None
    left = sorted(sec["name"] for sec in nevia.secrets.list())
    assert left == sorted([s.secret(s.ai.gateway_secret), s.postgres.secret, "OTHER"])
