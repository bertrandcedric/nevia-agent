import json
import re

import pytest
from nevia_kit.testing import FakeApi
from typer.testing import CliRunner

from acc import local as cli
from acc import steps
from acc.agent import BASE_PG_INIT_JOB, Agent, ai_env, prompt
from acc.config import AI_USER, Settings
from acc.run import RUN_JOB, STEPS, RunState
from acc.stack import RunNames
from core.nevia import NeviaError
from core.results import RESULT
from tests.fake_nevia import FakeCli, exec_env, job_ends

RUN = "261029120000"


def test_setup_wants_the_token_once_and_the_gateway_key_each_time(ag: Agent) -> None:
    wanted = {(w.name, w.always) for w in cli.Local(ag.settings, ag.nevia).bootstrap().wanted}
    assert wanted == {("GITHUB_TOKEN", False), ("AIVEN_AI_API_KEY", True)}


def _with_ai(settings: Settings, **update: object) -> Settings:
    return settings.model_copy(update={"ai": settings.ai.model_copy(update=update)})


def test_without_a_gateway_claude_uses_its_login(ag: Agent, settings: Settings) -> None:
    s = _with_ai(settings, claude=settings.ai.claude.model_copy(update={"gateway_url": ""}))
    assert not any(k.startswith("ANTHROPIC_") for k in ai_env(s))
    assert {w.name for w in cli.Local(s, ag.nevia).bootstrap().wanted} == {"GITHUB_TOKEN"}  # no gateway key wanted


def test_the_ai_gets_the_gateway_key_as_a_template_and_no_github_token(settings: Settings) -> None:
    env = ai_env(settings)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "{{secrets.ACC_AIVEN_AI_API_KEY}}"  # noqa: S105 - a template
    assert env["ANTHROPIC_BASE_URL"] == settings.ai.claude.gateway_url
    assert not any("GITHUB" in k for k in env)
    env = ai_env(_with_ai(settings, engine="copilot"))
    assert env["COPILOT_PROVIDER_API_KEY"] == "{{secrets.ACC_AIVEN_AI_API_KEY}}"
    assert not any("GITHUB" in k or k.startswith("ANTHROPIC_") for k in env)


def test_a_task_prompt_holds_the_request_and_the_check_path() -> None:
    text = prompt("task", "/home/u/acc/runs/1", "Add GET /x {id}", repo="o/r", branch="main")
    assert "`o/r`" in text and "`main`" in text and "Add GET /x {id}" in text
    migrate = prompt("migrate", "/home/u/acc/runs/1", repo="o/r", branch="main")
    assert "{{" not in migrate
    assert "## Commands" in text and "## Commands" in migrate  # the commands to run, as m2p's AGENTS.md has them
    assert "java -jar target/<jar>" in migrate and "sh -n /home/u/acc/runs/1/work/feature-check.sh" in text
    assert "Add GET /x" in text
    assert "/home/u/acc/runs/1/work/feature-check.sh" in text
    assert "{{" not in text


def test_clone_passes_the_token_for_the_job_only(ag: Agent, fake: FakeApi, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(steps, "HOME", tmp_path)
    job_ends(fake, "[clone] o/r (main) at abc\n")
    st = ag.start("migrate")
    props = tmp_path / f"acc/runs/{st.id}/repo/src/main/resources/application.properties"
    props.parent.mkdir(parents=True)
    props.write_text("database.kind = mongo\n")
    ag._clone(st)
    assert st.stack_kind == "mongo"  # read from the clone, as the sandbox reads it
    assert exec_env(fake, "remote step clone") == {"GITHUB_TOKEN": "{{secrets.ACC_GITHUB_TOKEN}}"}
    assert not any("GITHUB" in k for k in fake.env[fake.get("computers", ag.name)["id"]])


def test_the_ai_on_a_migration_gets_its_postgres_through_the_env(
    ag: Agent, fake: FakeApi, settings: Settings, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(steps, "HOME", tmp_path)
    monkeypatch.setenv("TZ", "Europe/Helsinki")
    st = RunState(id=RUN, kind="migrate", started="now")
    st.save()
    started: list[int] = []
    result = {"result": "<title>Migrate to Postgres</title><body>b</body>", "total_cost_usd": 1}
    rd = tmp_path / f"acc/runs/{RUN}"
    (rd / "logs").mkdir(parents=True)
    (rd / RESULT).write_text("")  # empty: no result yet, the AI starts

    def start_ai(_argv: object, _body: object) -> str:
        started.append(1)
        (rd / RESULT).write_text(json.dumps(result))  # the AI's, as its step writes it
        (rd / "logs/ai-1.log").write_text("13:34:05 says: on it\n")
        return "started\n"

    fake.on_exec("remote step ai", start_ai)
    fake.on_exec("status --porcelain", " M pom.xml\n")
    job_ends(fake)
    ag._ai(st)
    env = exec_env(fake, "remote step ai")
    job = next(s for s in fake.scripts() if "remote step ai" in s and "nohup" in s)
    scripts = fake.scripts()
    lent = f"~/acc/runs/{RUN}/repo ~/acc/runs/{RUN}/work"  # to the AI's own user, then back
    given = next(i for i, s in enumerate(scripts) if f"chown -R acc-ai: {lent}" in s)
    back = next(i for i, s in enumerate(scripts) if f"chown -R $(id -u):$(id -g) {lent}" in s)
    assert given < scripts.index(job) < back
    assert env["DATABASE_URL"] == f"{{{{secrets.ACC_DATABASE_URL_DEV_{RUN}}}}}"
    assert env["PG_CA_FILE"] == f"{rd}/dev-ca.pem" and env["TZ"] == "Europe/Helsinki"
    assert str(rd) in fake.files[ag.name, f"acc/runs/{RUN}/prompt.md"].decode()  # the run's directory, here
    assert not any("GITHUB" in k or k.startswith("NEVIA") for k in env)
    assert st.title == "Migrate to Postgres"
    assert json.loads((st.path / RESULT).read_text()) == result  # copied into the run's own
    assert ag.activity(RUN) == ["13:34:05 says: on it"] and ag.activity(RUN, 1) == []
    assert len(started) == 1
    assert not fake.find("databases", f"{settings.app.computer}-db-{RUN}")  # made for the AI, deleted after
    assert not fake.find("secrets", f"ACC_DATABASE_URL_DEV_{RUN}")


def test_tools_are_read_only(ag: Agent, fake: FakeApi) -> None:
    ag.upload_tools(ag.computer())
    for name in ("render-claude.jq", "render-copilot.jq", "common.jq"):
        assert (ag.name, f"acc/tools/{name}") in fake.files
    scripts = fake.scripts(ag.name)
    assert any("chmod 444 acc/tools/render-claude.jq.tmp" in s for s in scripts)


def test_clean_deletes_only_what_the_run_left_sandboxes_first(ag: Agent, fake: FakeApi, settings: Settings) -> None:
    other = "261029130000"
    app = fake.get("computers", settings.app.computer)
    for run in (RUN, other):
        fake.add("computers", f"acc-app-{run}")
        fake.add("computers", f"acc-mongo-{run}")
        fake.add("databases", f"acc-pg-{run}")
        fake.add("secrets", f"ACC_DATABASE_URL_{run}")
        fake.add("checkpoints", f"acc-{run}", source=app["id"])
    fake.add("databases", "acc-pg")
    fake.add("computers", "acc-app-notes")
    d = ag.cleanup.doomed(RUN)
    assert d.computers == [f"acc-app-{RUN}", f"acc-mongo-{RUN}"]
    assert d.checkpoints == [("acc-app", f"acc-{RUN}")]
    assert d.databases == [f"acc-pg-{RUN}"]
    assert d.secrets == [f"ACC_DATABASE_URL_{RUN}"]
    ag.cleanup.delete(d)
    ops = [c.op for c in fake.calls if c.op.startswith("delete")]
    assert ops.index("deleteComputer") < ops.index("deleteComputerCheckpoint")
    assert fake.find("computers", f"acc-app-{other}") and fake.find("databases", "acc-pg")
    assert fake.find("computers", settings.app.computer)


def test_destroy_takes_the_stack_but_not_the_agent(ag: Agent, fake: FakeApi, settings: Settings) -> None:
    fake.add("computers", f"acc-app-{RUN}")
    fake.add("databases", "acc-pg")
    fake.add("secrets", "ACC_GITHUB_TOKEN")
    fake.add("secrets", "GITHUB_TOKEN")  # someone else's: no prefix
    d = ag.cleanup.doomed_all()
    assert d.computers == [f"acc-app-{RUN}", "acc-app", "acc-mongo"]  # acc-agent is your machine's to delete
    assert d.databases == ["acc-pg"]
    assert d.secrets == []  # ACC_GITHUB_TOKEN too


def test_each_step_of_a_run_is_done_by_one_method(ag: Agent) -> None:
    assert set(ag.dispatch) == {step for order in STEPS.values() for step in order}


def test_redo_forgets_a_step_and_the_ones_after(ag: Agent) -> None:
    st = RunState(id=RUN, kind="task", started="now", done=["clone", "stack", "ai", "build", "sandbox"])
    ag.redo(st, "sandbox")
    assert st.done == ["clone", "stack", "ai", "build"]
    assert RunState.load(RUN).done == st.done
    with pytest.raises(ValueError, match="no step 'analysis' in a task run"):
        ag.redo(st, "analysis")


def test_run_wants_a_mode_and_from_goes_with_resume_only() -> None:
    assert CliRunner().invoke(cli.app, ["run"]).exit_code == 2  # no default mode
    assert CliRunner().invoke(cli.app, ["run", "fix"]).exit_code == 2
    result = CliRunner().invoke(cli.app, ["run", "migrate", "--from", "sandbox"])
    assert result.exit_code == 2 and "--from goes with resume" in result.output


def test_the_app_mongo_runs_the_shared_script_with_auth_chisel_and_its_seed(ag: Agent, fake: FakeApi) -> None:
    job_ends(fake)
    ag.stack.mongo_up("acc-mongo-x", init=b"seed")
    env = fake.files["acc-mongo-x", "mongo.env"].decode()
    assert "MONGO_IMAGE=docker.io/library/mongo:8.0.34" in env  # [mongodb] image, the same as m2p's
    assert "MONGO_PUBLISH=1" in env and "CHISEL_VERSION=1.10.1" in env and "MONGO_DB=hello" in env
    assert "MONGO_POD=acc\n" in env and "MONGO_CONTAINER=acc-mongo\n" in env
    stored = fake.env[fake.get("computers", "acc-mongo-x")["id"]]
    assert stored["MONGO_ROOT_PASSWORD"] == "{{secrets.ACC_MONGO_ROOT_PASSWORD}}"  # noqa: S105 - a template
    assert ("acc-mongo-x", ".nevia-agent/mongo-up.sh") in fake.files
    assert any("bash ~/.nevia-agent/mongo-up.sh" in s for s in fake.scripts())
    assert any("rm -rf mongo-init" in s for s in fake.scripts())  # local/mongo-init, for the image's first start


def test_a_run_s_names_are_made_and_matched_in_one_place(settings: Settings) -> None:
    n = RunNames(settings, RUN)
    assert n.checkpoint == f"{settings.prefix}{RUN}" and n.sandbox == f"{settings.app.computer}-{RUN}"
    assert (n.pg_secret, n.dev_secret) == (f"ACC_DATABASE_URL_{RUN}", f"ACC_DATABASE_URL_DEV_{RUN}")
    anyone = RunNames(settings)
    assert anyone.matches(n.dev_db, "pg", "dev_db") == RUN
    assert anyone.matches(n.dev_db, "sandbox") is None  # a database's name, not a computer's
    assert RunNames(settings, "261029130000").matches(n.sandbox, "sandbox") is None  # another run's
    assert anyone.matches(f"{settings.app.computer}-notes", "sandbox") is None
    assert anyone.matches(f"x{n.pg_secret}", "pg_secret") is None


def test_clean_lists_what_a_run_left_and_asks_before_deleting(ag: Agent, fake: FakeApi, monkeypatch) -> None:
    deleted: list[str] = []
    fake.on_exec("remote clean --yes", lambda _a, _b: deleted.append("clean") or "")
    listed = [f'{{"computers": ["acc-app-{RUN}"]}}\n']
    fake.on_exec("remote clean", lambda _a, _b: listed[-1])
    monkeypatch.setattr(cli, "_agent", lambda: cli.Local(ag.settings, ag.nevia))
    result = CliRunner().invoke(cli.app, ["clean"], input="n\n")
    assert f"computers: acc-app-{RUN}" in result.output and not deleted
    result = CliRunner().invoke(cli.app, ["clean", "--yes"])
    assert result.exit_code == 0, result.output
    assert deleted == ["clean"]
    listed.append('{"dirs": ["acc/runs"]}\n')  # files only: nothing to ask about
    result = CliRunner().invoke(cli.app, ["clean"])
    assert "nothing of a run" in result.output and deleted == ["clean"]


def test_the_collected_state_is_read_where_it_was_collected(nevia, settings: Settings, monkeypatch, tmp_path) -> None:
    dest = tmp_path / "collected" / RUN
    dest.mkdir(parents=True)
    (dest / "state.json").write_text(RunState(id=RUN, kind="migrate", started="now", outcome="ok").model_dump_json())
    monkeypatch.setattr(cli, "latest_run", lambda _pc: RUN)
    monkeypatch.setattr(cli, "collect", lambda _s, _pc, _run: dest)
    st = cli._collect(cli.Local(settings, nevia))
    assert st is not None and (st.id, st.outcome) == (RUN, "ok")


def test_stop_stops_the_orchestrator_then_the_run_s_jobs_and_the_ai_s_processes(ag: Agent, fake: FakeApi) -> None:
    RunState(id=RUN, kind="task", started="now").save()
    fake.on_exec(f"ls -1 *-{RUN}.log", f"ai-{RUN}.log\nclone-{RUN}.log\n")
    ag.stop()
    scripts = fake.scripts()
    stopped = [m[1] for s in scripts if (m := re.search(r"read -r s boot < \$HOME/\.acc-jobs/(\S+)\.pid", s))]
    assert stopped == [RUN_JOB, BASE_PG_INIT_JOB, f"ai-{RUN}", f"clone-{RUN}"]
    assert "pkill -f" not in "\n".join(scripts)
    assert f"sudo -n pkill -u {AI_USER} || true" in scripts[-1]
    assert RunState.load(RUN).outcome == "stopped"


def test_the_stack_goes_after_the_earlier_runs_sandboxes_the_run_s_own_stays(
    ag: Agent, fake: FakeApi, settings: Settings, tmp_path, monkeypatch
) -> None:
    earlier, n = "261029110000", RunNames(settings, RUN)
    ag.nevia.checkpoints.create(settings.app.computer, f"acc-{earlier}")
    ag.nevia.checkpoints.restore(settings.app.computer, f"acc-{earlier}", f"acc-app-{earlier}")
    ag.nevia.computers.branch(settings.mongo.computer, f"acc-mongo-{earlier}")
    fake.add("computers", n.mongo_fork)  # this run's, from an earlier try of it
    fake.add("computers", "acc-app-notes")  # no run's
    with pytest.raises(NeviaError, match="in use by 1 live computer"):  # as Nevia refuses it
        ag.nevia.computers.delete(settings.app.computer)
    cli = ag.nevia.run
    assert isinstance(cli, FakeCli)
    before = len(cli.calls)
    (tmp_path / "src").mkdir()
    (tmp_path / "pom.xml").write_text("<project/>")
    monkeypatch.setattr(ag.stack, "app_up", lambda name, *_a: f"https://{name}.example")
    assert ag.stack.deploy(tmp_path, "postgres", lambda _sec: None, RUN) == "https://acc-app.example"
    deleted = [c[1] for c in cli.calls[before:] if c[0] == "rm"]
    assert deleted == [f"acc-app-{earlier}", f"acc-mongo-{earlier}", settings.app.computer, settings.mongo.computer]
    assert not fake.find("computers", f"acc-app-{earlier}") and not fake.find("computers", f"acc-mongo-{earlier}")
    assert fake.find("computers", n.mongo_fork) and fake.find("computers", "acc-app-notes")


def test_the_app_goes_through_the_pooler_when_asked_the_ai_s_postgres_never(
    ag: Agent, fake: FakeApi, settings: Settings
) -> None:
    pooled = settings.model_copy(update={"postgres": settings.postgres.model_copy(update={"pooler": True})})
    ag.stack.settings = pooled
    ag.stack.postgres_up("acc-pg", "ACC_DATABASE_URL", app=True)
    ag.stack.postgres_up(f"acc-app-db-{RUN}", f"ACC_DATABASE_URL_DEV_{RUN}")
    values = {c.body["name"]: c.body["value"] for c in fake.calls if c.op == "createSecret"}
    assert values["ACC_DATABASE_URL"] == "postgres://app:secret@acc-pg.db.example:6432/app?prepareThreshold=0"
    assert ":5432/" in values[f"ACC_DATABASE_URL_DEV_{RUN}"] and "prepare" not in values[f"ACC_DATABASE_URL_DEV_{RUN}"]
    assert steps.libpq_uri(values["ACC_DATABASE_URL"]) == "postgres://app:secret@acc-pg.db.example:6432/app"
    assert steps.libpq_uri("postgres://u:p@h:6432/d?sslmode=require&prepareThreshold=0") == (
        "postgres://u:p@h:6432/d?sslmode=require"
    )


def test_waiting_for_a_sandbox_s_dns_is_one_command_on_it(ag: Agent, fake: FakeApi) -> None:
    fake.add("computers", "acc-app-1")
    fake.on_exec("getent hosts", "ok\n")
    ag.stack.wait_dns("acc-app-1", "app.example")
    waits = [s for s in fake.scripts() if "getent hosts" in s]
    assert len(waits) == 1  # one exec, one sign-in: the lookups loop on the computer
    assert "seq 120" in waits[0] and "app.example" in waits[0]


def test_a_sandbox_without_dns_times_out(ag: Agent, fake: FakeApi) -> None:
    fake.add("computers", "acc-app-1")
    fake.on_exec("getent hosts", "no\n")
    with pytest.raises(TimeoutError, match=r"no DNS for app\.example"):
        ag.stack.wait_dns("acc-app-1", "app.example")
