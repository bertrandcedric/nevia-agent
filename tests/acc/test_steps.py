import io
import json
import pwd
import tarfile
from pathlib import Path

import httpx
import pytest

from acc import steps
from acc.agent import pack
from acc.config import Settings


def test_db_kind_is_the_last_database_kind() -> None:
    assert steps.db_kind("a=1\ndatabase.kind = mongo \n") == "mongo"
    assert steps.db_kind("database.kind: mongo\ndatabase.kind=postgres\n") == "postgres"
    assert steps.db_kind("other=1\n") == ""


def test_the_analysis_loses_its_sample_values(tmp_path: Path) -> None:
    c = tmp_path / "collections" / "customers"
    c.mkdir(parents=True)
    (c / "customers.json").write_text(json.dumps({"fields": {"email": {"types": ["string"], "values": ["a@b"]}}}))
    (c / "customers.stats.yaml").write_text(
        "documents_in_collection: 5000\ndepth_max: 3\narray_field_count: 4\nmigrability_score: 90\n"
        "warnings:\n  - field_path: orders.date\n    examples:\n      - '12/03/2024'\n    kind: type_mix\n"
    )
    steps.strip_values(tmp_path)
    assert "a@b" not in (c / "customers.json").read_text()
    stats = (c / "customers.stats.yaml").read_text()
    assert "12/03/2024" not in stats and "kind: type_mix" in stats and "field_path: orders.date" in stats
    assert "| `customers` | 5000 | 3 | 4 | 90 | `orders.date` |" in steps.summary(tmp_path / "collections")


def test_pg_env_decodes_the_uri() -> None:
    env = steps.pg_env("postgres://u%40x:p%2Fw@db.example/hello?sslmode=require", "/ca.pem")
    assert env == {"PGHOST": "db.example", "PGPORT": "5432", "PGUSER": "u@x", "PGPASSWORD": "p/w",
                   "PGDATABASE": "hello", "PGSSLMODE": "verify-full", "PGSSLROOTCERT": "/ca.pem"}  # fmt: skip


def test_the_ai_runs_as_its_user_with_the_names_of_its_env_not_their_values(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(pwd, "getpwnam", lambda _user: type("P", (), {"pw_dir": "/home/acc-ai"})())
    (tmp_path / "prompt.md").write_text("the task")
    cmd = steps.ai_command(settings, tmp_path, {"ANTHROPIC_AUTH_TOKEN": "s3cr3t", "PGPASSWORD": "pw"})
    assert cmd[:5] == ["sudo", "-n", "-u", "acc-ai", "-H"]
    assert "--preserve-env=ANTHROPIC_AUTH_TOKEN,PGPASSWORD" in cmd
    assert "s3cr3t" not in " ".join(cmd) and "pw" not in cmd  # in the env sudo keeps, never in the command line
    assert "/home/acc-ai/.local/bin/claude" in cmd and "the task" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == ",".join(settings.agent.allowed_tools)


def test_app_jar_is_the_runnable_one(tmp_path: Path) -> None:
    (tmp_path / "target").mkdir()
    for name in ("app.jar", "original-app.jar", "app-sources.jar"):
        (tmp_path / "target" / name).write_bytes(b"")
    assert steps.app_jar(tmp_path).name == "app.jar"
    with pytest.raises(RuntimeError, match="no jar"):
        steps.app_jar(tmp_path / "none")


def test_pack_takes_files_from_disk_and_bytes(tmp_path: Path) -> None:
    (tmp_path / "src" / "main").mkdir(parents=True)
    (tmp_path / "src" / "main" / "App.java").write_text("class App {}")
    (tmp_path / "pom.xml").write_text("<project/>")
    data = pack({"pom.xml": tmp_path / "pom.xml", "src": tmp_path / "src", "ca.pem": b"CA"})
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        assert {"pom.xml", "src/main/App.java", "ca.pem"} <= set(tar.getnames())


def _github(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_the_pull_request_follows_a_redirect_and_finds_an_open_one(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t")

    def moved(req: httpx.Request) -> httpx.Response:
        if "o/old" in req.url.path:
            return httpx.Response(307, headers={"location": str(req.url).replace("o/old", "o/new")})
        return httpx.Response(201, json={"html_url": "https://github.com/o/new/pull/1"})

    assert (
        steps.pull_request("o/old", "agent/1", "main", "T\nB", http=_github(moved)) == "https://github.com/o/new/pull/1"
    )

    def exists(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(422, json={"message": "Validation Failed: A pull request already exists"})
        assert req.url.params["head"] == "o:agent/1"
        return httpx.Response(200, json=[{"html_url": "https://github.com/o/r/pull/7"}])

    assert steps.pull_request("o/r", "agent/1", "main", "T\nB", http=_github(exists)) == "https://github.com/o/r/pull/7"

    def refused(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})

    with pytest.raises(RuntimeError, match=r"403 .*not accessible"):
        steps.pull_request("o/r", "agent/1", "main", "T\nB", http=_github(refused))


def test_the_ai_gets_the_cli_and_a_copy_of_the_key_of_its_own(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(pwd, "getpwnam", lambda _user: type("P", (), {"pw_dir": "/home/acc-ai"})())
    ran: list[list[str]] = []
    monkeypatch.setattr(steps, "run", lambda args, **_kw: ran.append(args) or "")
    steps.lend_nevia(settings)
    key = f"/home/acc-ai/{settings.agent.key_path}"
    assert ran[0][-4:] == ["-m", "755", f"{steps.HOME}/.local/bin/nevia", "/home/acc-ai/.local/bin/nevia"]
    assert ran[1][:3] == ["sudo", "-n", "install"] and ran[1][-3:] == [
        "600",
        f"{steps.HOME}/{settings.agent.key_path}",
        key,
    ]
    assert f"auth use-key-file {key}" in ran[2][-1] and "workspace use default" in ran[2][-1]
