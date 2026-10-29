import json

import pytest
from nevia_kit.testing import FakeApi
from typer.testing import CliRunner

from acc import local as cli
from acc.agent import Agent, RunState, claude_env, prompt
from acc.config import Settings

RUN = "261029120000"


def _job_ends(fake: FakeApi, output: str = "", code: str = "0") -> None:
    fake.on_exec(".exit 2>/dev/null; echo", f"{code}\n{output.splitlines()[-1] if output else ''}\n")
    fake.on_exec("tail -n 300", output)


def _env_of(fake: FakeApi, needle: str) -> dict[str, str]:
    return next(body.get("env") or {} for _, argv, body in fake.execs if needle in " ".join(argv))


def test_setup_wants_the_token_once_and_the_gateway_key_each_time(ag: Agent) -> None:
    wanted = {(w.var, w.always) for w in cli.bootstrap(ag.settings, ag.nevia).wanted}
    assert wanted == {("GITHUB_TOKEN", False), ("AIVEN_AI_API_KEY", True)}


def test_without_a_gateway_claude_uses_its_login(settings: Settings) -> None:
    claude = settings.claude.model_copy(update={"gateway_url": ""})
    env = claude_env(settings.model_copy(update={"claude": claude}))
    assert not any(k.startswith("ANTHROPIC_") for k in env)


def test_claude_gets_the_gateway_key_as_a_template_and_no_github_token(settings: Settings) -> None:
    claude = settings.claude.model_copy(update={"model": "m"})
    env = claude_env(settings.model_copy(update={"claude": claude}))
    assert env["ANTHROPIC_AUTH_TOKEN"] == "{{secrets.ACC_AIVEN_AI_API_KEY}}"  # noqa: S105 - a template
    assert env["ANTHROPIC_BASE_URL"] == settings.claude.gateway_url
    assert not any("GITHUB" in k for k in env)


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


def test_clone_passes_the_token_for_the_job_only(ag: Agent, fake: FakeApi) -> None:
    _job_ends(fake, "[clone] o/r (main) at abc\n")
    st = ag.start("migrate")
    props = f"acc/runs/{st.id}/repo/src/main/resources/application.properties"
    fake.files[ag.name, props] = b"database.kind = mongo\n"
    ag._clone(st)
    assert st.stack_kind == "mongo"  # read from the clone, as the sandbox reads it
    assert _env_of(fake, "remote step clone") == {"GITHUB_TOKEN": "{{secrets.ACC_GITHUB_TOKEN}}"}
    assert not any("GITHUB" in k for k in fake.env[fake.get("computers", ag.name)["id"]])


def test_claude_on_a_migration_gets_its_postgres_through_the_env(ag: Agent, fake: FakeApi, settings: Settings) -> None:
    st = RunState(id=RUN, kind="migrate", started="now")
    st.save()
    started: list[int] = []
    result = {"result": "<title>Migrate to Postgres</title><body>b</body>", "total_cost_usd": 1}
    fake.files[ag.name, f"acc/runs/{RUN}/result.json"] = json.dumps(result).encode()
    fake.on_exec("echo $HOME", "/home/nevia\n")
    fake.on_exec("result.json && echo yes", lambda _argv, _body: "yes\n" if started else "")
    fake.on_exec("remote step claude", lambda _argv, _body: started.append(1) or "started\n")
    fake.on_exec("status --porcelain", " M pom.xml\n")
    _job_ends(fake)
    ag._claude(st)
    env = _env_of(fake, "remote step claude")
    job = next(s for s in fake.scripts() if "remote step claude" in s and "nohup" in s)
    scripts = fake.scripts()
    lent = f"~/acc/runs/{RUN}/repo ~/acc/runs/{RUN}/work"  # to Claude's own user, then back
    given = next(i for i, s in enumerate(scripts) if f"chown -R acc-claude: {lent}" in s)
    back = next(i for i, s in enumerate(scripts) if f"chown -R $(id -u):$(id -g) {lent}" in s)
    assert given < scripts.index(job) < back
    assert env["DATABASE_URL"] == f"{{{{secrets.ACC_DATABASE_URL_DEV_{RUN}}}}}"
    assert env["PG_CA_FILE"] == f"/home/nevia/acc/runs/{RUN}/dev-ca.pem"
    assert not any("GITHUB" in k or k.startswith("NEVIA") for k in env)
    assert st.title == "Migrate to Postgres"
    assert len(started) == 1
    assert not fake.find("databases", f"{settings.app.computer}-db-{RUN}")  # made for Claude, deleted after
    assert not fake.find("secrets", f"ACC_DATABASE_URL_DEV_{RUN}")


def test_tools_are_read_only(ag: Agent, fake: FakeApi) -> None:
    ag.upload_tools(ag.computer())
    assert (ag.name, "acc/tools/render.jq") in fake.files and (ag.name, "acc/tools/common.jq") in fake.files
    scripts = fake.scripts(ag.name)
    assert any("chmod 444 acc/tools/render.jq.tmp" in s for s in scripts)


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
    d = ag.doomed(RUN)
    assert d.computers == [f"acc-app-{RUN}", f"acc-mongo-{RUN}"]
    assert d.checkpoints == [("acc-app", f"acc-{RUN}")]
    assert d.databases == [f"acc-pg-{RUN}"]
    assert d.secrets == [f"ACC_DATABASE_URL_{RUN}"]
    ag.delete(d)
    ops = [c.op for c in fake.calls if c.op.startswith("delete")]
    assert ops.index("deleteComputer") < ops.index("deleteComputerCheckpoint")
    assert fake.find("computers", f"acc-app-{other}") and fake.find("databases", "acc-pg")
    assert fake.find("computers", settings.app.computer)


def test_destroy_takes_the_stack_but_not_the_agent(ag: Agent, fake: FakeApi, settings: Settings) -> None:
    fake.add("computers", f"acc-app-{RUN}")
    fake.add("databases", "acc-pg")
    fake.add("secrets", "ACC_GITHUB_TOKEN")
    fake.add("secrets", "GITHUB_TOKEN")  # someone else's: no prefix
    d = ag.doomed_all()
    assert d.computers == [f"acc-app-{RUN}", "acc-app", "acc-mongo"]  # acc-agent is your machine's to delete
    assert d.databases == ["acc-pg"]
    assert d.secrets == []  # ACC_GITHUB_TOKEN too


def test_redo_forgets_a_step_and_the_ones_after(ag: Agent) -> None:
    st = RunState(id=RUN, kind="task", started="now", done=["clone", "stack", "claude", "build", "sandbox"])
    ag.redo(st, "sandbox")
    assert st.done == ["clone", "stack", "claude", "build"]
    assert RunState.load(RUN).done == st.done
    with pytest.raises(ValueError, match="no step 'analysis' in a task run"):
        ag.redo(st, "analysis")


def test_run_wants_a_mode_and_from_goes_with_resume_only() -> None:
    assert CliRunner().invoke(cli.app, ["run"]).exit_code == 2  # no default mode
    assert CliRunner().invoke(cli.app, ["run", "fix"]).exit_code == 2
    result = CliRunner().invoke(cli.app, ["run", "migrate", "--from", "sandbox"])
    assert result.exit_code == 2 and "--from goes with resume" in result.output


def test_the_app_mongo_runs_the_shared_script_with_auth_chisel_and_its_seed(ag: Agent, fake: FakeApi) -> None:
    _job_ends(fake)
    ag.stack.mongo_up("acc-mongo-x", init=b"seed")
    env = fake.files["acc-mongo-x", "mongo.env"].decode()
    assert "MONGO_IMAGE=docker.io/library/mongo:8.0.34" in env  # [images] mongo, the same as m2p's
    assert "MONGO_PUBLISH=1" in env and "CHISEL_VERSION=1.10.1" in env and "MONGO_DB=hello" in env
    stored = fake.env[fake.get("computers", "acc-mongo-x")["id"]]
    assert stored["MONGO_ROOT_PASSWORD"] == "{{secrets.ACC_MONGO_ROOT_PASSWORD}}"  # noqa: S105 - a template
    assert ("acc-mongo-x", ".nevia-agent/mongo-up.sh") in fake.files
    assert any("bash ~/.nevia-agent/mongo-up.sh" in s for s in fake.scripts())
    assert any("rm -rf mongo-init" in s for s in fake.scripts())  # local/mongo-init, for the image's first start
