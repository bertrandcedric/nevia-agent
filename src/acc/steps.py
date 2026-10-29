"""A run's work on the agent computer, the steps `acc.agent` runs: the clone, the mongo2pg analysis, the AI, the
build, the seed data, the pull request, and the computer's setup.

Each runs here, as a subprocess. The ones that need a secret (the GitHub token, the gateway key, a database's
URI) run as `agent acc remote step ...` jobs, through Nevia's exec, the one way its template gets its value; it
is in their env, for them only.
"""

import json
import os
import pwd
import re
import shutil
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from acc.config import AI_USER, Settings
from core.agent import ai
from core.agent.host import HOME, lib, q, run
from core.shell import console

CHECK = "work/feature-check.sh"  # a task's feature check, in the run's directory
_KIND = re.compile(r"^\s*database\.kind\s*[=:]\s*(\S*)\s*$", re.MULTILINE)
_JAR_EXCLUDED = ("original-", "-sources.jar", "-javadoc.jar")


def db_kind(properties: str) -> str:
    """`database.kind` (mongo or postgres) in an application.properties, or nothing."""
    found = _KIND.findall(properties)
    return found[-1] if found else ""


def run_dir(s: Settings, rid: str) -> Path:
    return HOME / s.agent.dir / "runs" / rid


def tools_env(s: Settings) -> dict[str, str]:
    """This user's JDK and Maven on PATH, by absolute path, so that the AI's user finds them too."""
    d = HOME / s.agent.dir
    return {"JAVA_HOME": str(d / "jdk"), "PATH": f"{d}/maven/bin:{d}/jdk/bin:{os.environ.get('PATH', '')}"}


# ---------------------------------------------------------------------- git, with the token from the env


def git(*args: str, cwd: Path | None = None) -> str:
    """git with GITHUB_TOKEN (this step's env), never written into .git/config."""
    helper = '!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f'
    return run(["git", "-c", "credential.helper=", "-c", f"credential.helper={helper}", *args], cwd=cwd)


def clone(s: Settings, rid: str) -> None:
    """The base branch into the run's `repo`, a Maven app with a database.kind; a dry-run push checks first
    that the token may write `agent/<run>`, rather than after the AI's work."""
    r, repo = s.repo, run_dir(s, rid) / "repo"
    shutil.rmtree(repo, ignore_errors=True)
    git("clone", "-q", "--branch", r.base_branch, f"https://github.com/{r.github}.git", str(repo))
    git("config", "user.name", "acc agent", cwd=repo)
    git("config", "user.email", "acc-agent@users.noreply.github.com", cwd=repo)
    props = repo / "src/main/resources/application.properties"
    if not (repo / "pom.xml").is_file() or not db_kind(props.read_text() if props.is_file() else ""):
        raise RuntimeError(f"{r.github} is not a Maven app with a database.kind")
    try:
        git("push", "-q", "--dry-run", "origin", f"HEAD:refs/heads/agent/{rid}", cwd=repo)
    except RuntimeError:
        raise RuntimeError(f"the GitHub token can't push to {r.github}: give it Contents and Pull requests") from None
    console.log(f"{r.github} ({r.base_branch}) at {git('rev-parse', '--short', 'HEAD', cwd=repo).strip()}")


def open_pr(s: Settings, rid: str) -> str:
    """The AI's change committed on `agent/<run>`, pushed, and a pull request into the base branch, its title
    the first line of pr.md and its body the rest; its URL. Never on the base branch, never merged."""
    r, d = s.repo, run_dir(s, rid)
    branch, repo = f"agent/{rid}", d / "repo"
    title = (d / "pr.md").read_text().partition("\n")[0]
    git("checkout", "-q", "-B", branch, cwd=repo)
    git("add", "-A", cwd=repo)
    if run(["git", "status", "--porcelain"], cwd=repo).strip():
        git("commit", "-q", "-m", title, "-m", f"Proposed by {s.ai.name} running on a Nevia computer.", cwd=repo)
    git("push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}", cwd=repo)
    return pull_request(r.github, branch, r.base_branch, (d / "pr.md").read_text())


def pull_request(github: str, head: str, base: str, text: str, *, http: httpx.Client | None = None) -> str:
    """The URL of a pull request of `head` into `base`, `text`'s first line its title and the rest its body: a
    new one, or the one already open (a run taken up). GitHub's redirects are followed (a renamed or moved
    repository); any other answer fails with its message."""
    title, _, body = text.partition("\n")
    body = body.strip()
    headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
    with http or httpx.Client(follow_redirects=True, timeout=60) as gh:
        url = f"https://api.github.com/repos/{github}/pulls"
        answer = gh.post(url, headers=headers, json={"title": title, "head": head, "base": base, "body": body})
        if answer.status_code == 422 and "already exists" in answer.text:
            owner = github.split("/", 1)[0]
            found = gh.get(url, headers=headers, params={"head": f"{owner}:{head}", "state": "open"}).json()
            if isinstance(found, list) and found:
                return found[0]["html_url"]
        data = answer.json() if "json" in answer.headers.get("content-type", "") else {}
        if answer.is_error or not isinstance(data, dict) or "html_url" not in data:
            why = data.get("message", answer.text[:300]) if isinstance(data, dict) else answer.text[:300]
            raise RuntimeError(f"{head} pushed, but GitHub answered {answer.status_code} to the pull request: {why}")
        return data["html_url"]


# ---------------------------------------------------------------------- the analysis


def strip_values(out: Path) -> None:
    """What the AI may read of an analysis: no sample value, in the schemas (`values`) or the infer warnings'
    `examples:` (the lines under them, more indented or list items)."""

    def drop(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: drop(v) for k, v in node.items() if k != "values"}
        return [drop(v) for v in node] if isinstance(node, list) else node

    for f in out.rglob("*.json"):
        f.write_text(json.dumps(drop(json.loads(f.read_text())), indent=2) + "\n")
    for f in out.rglob("*.stats.yaml"):
        kept, skip = [], None
        for line in f.read_text().splitlines():
            ind = len(line) - len(line.lstrip(" "))
            if skip is not None and (ind > skip or line[ind:].startswith("- ")):
                continue
            skip = ind if line.lstrip().startswith("examples:") else None
            if skip is None:
                kept.append(line)
        f.write_text("\n".join(kept) + "\n")


def summary(collections: Path) -> str:
    """A table of the analysed collections, for the pull request."""
    rows = [
        "| Collection | Documents | Max depth | Array fields | Score | Infer warnings |",
        "|---|---|---|---|---|---|",
    ]
    for f in sorted(collections.glob("*/*.stats.yaml")):
        text = f.read_text()

        def field(key: str, text: str = text) -> str:
            m = re.search(rf"^{key}:\s*(\S+)", text, re.MULTILINE)
            return m[1] if m else ""

        warnings = (
            ", ".join(f"`{p}`" for p in re.findall(r"^\s*-?\s*field_path:\s*(\S+)", text, re.MULTILINE)) or "none"
        )
        counts = [field(k) for k in ("documents_in_collection", "depth_max", "array_field_count", "migrability_score")]
        rows.append(f"| `{f.name.removesuffix('.stats.yaml')}` | {' | '.join(counts)} | {warnings} |")
    return "\n".join(rows) + "\n"


def analyse(s: Settings, rid: str) -> None:
    """mongo2pg init and infer on the app's MongoDB (MONGO_URI, through a link) into the run's `mongo2pg`, then
    what the AI may read of it in `analysis/`, read-only, and its summary. The project goes: its config holds
    the URIs, its HTML reports the values."""
    d, binary = run_dir(s, rid), HOME / s.agent.dir / "bin/mongo2pg"
    project, out = d / "mongo2pg", d / "analysis"
    if out.exists():
        run(["chmod", "-R", "u+w", str(out)])
    shutil.rmtree(out, ignore_errors=True)
    shutil.rmtree(project, ignore_errors=True)
    try:
        uris = ["--source-uri", os.environ["MONGO_URI"], "--target-uri", os.environ["DATABASE_URL"]]
        run([str(binary), "init", "--project-base", str(project), "--project-name", "analysis",
             "--namespace", s.mongo.database, *uris])  # fmt: skip
        run([str(binary), "infer", "-c", str(project / "analysis/config/analysis.toml")], log=d / "logs/analysis.log")
        shutil.copytree(project / "analysis/source/collections", out / "collections")
        if (tables := project / "analysis/schema/tables").is_dir():
            shutil.copytree(tables, out / "tables")
    finally:
        shutil.rmtree(project, ignore_errors=True)
    for hidden in list(out.rglob(".*")):
        shutil.rmtree(hidden, ignore_errors=True) if hidden.is_dir() else hidden.unlink(missing_ok=True)
    strip_values(out)
    (out / "summary.md").write_text(summary(out / "collections"))
    run(["chmod", "-R", "a-w", str(out)])
    console.log(f"{sum(1 for p in (out / 'collections').iterdir() if p.is_dir())} collection(s) analysed")


# ---------------------------------------------------------------------- the AI, the build, the seed data


def pg_env(uri: str, ca_file: str) -> dict[str, str]:
    """The PG* variables of a libpq URI (user and password URL-decoded), so that psql needs no arguments."""
    p = urllib.parse.urlsplit(uri)
    return {
        "PGHOST": p.hostname or "", "PGPORT": str(p.port or 5432), "PGUSER": urllib.parse.unquote(p.username or ""),
        "PGPASSWORD": urllib.parse.unquote(p.password or ""), "PGDATABASE": p.path.lstrip("/"),
        "PGSSLMODE": "verify-full", "PGSSLROOTCERT": ca_file,
    }  # fmt: skip


def ai_command(s: Settings, d: Path, env: dict[str, str]) -> list[str]:
    """The AI headless, as its own Unix user (AI_USER): it gets `env` (sudo keeps those names only), not this
    user's files, its key file or its env."""
    user_home = Path(pwd.getpwnam(AI_USER).pw_dir)
    other = d / "analysis" if (d / "analysis").is_dir() else d / "work"
    keep = ",".join(sorted(env))
    path = f"{user_home}/{ai.BIN}:{tools_env(s)['PATH']}"
    cmd = ai.command(s.ai, (d / "prompt.md").read_text(), home=user_home, allowed=s.agent.allowed_tools,
                     dirs=(other, Path("/tmp")))  # fmt: skip  # noqa: S108 - its scratch space
    return ["sudo", "-n", "-u", AI_USER, "-H", f"--preserve-env={keep}", "env", f"PATH={path}",
            f"JAVA_HOME={tools_env(s)['JAVA_HOME']}", *cmd]  # fmt: skip


def ai_session(s: Settings, rid: str) -> None:
    """The AI on the clone: its events rendered into logs/ai.log as they come (and kept in ai.jsonl), its result
    into result.json. Its env: this step's (the gateway, a migration's DATABASE_URL and PG_CA_FILE,
    and their PG* variables)."""
    d = run_dir(s, rid)
    (d / "work").mkdir(exist_ok=True)
    (d / "logs").mkdir(exist_ok=True)
    env = {
        k: v
        for k, v in os.environ.items()
        if k.startswith(ai.ENV_PREFIXES) or k in {"DATABASE_URL", "PG_CA_FILE", "TZ"}
    }
    if "DATABASE_URL" in env:
        env |= pg_env(env["DATABASE_URL"], env["PG_CA_FILE"])
    log, render = d / "logs/ai.log", HOME / s.agent.dir / "tools" / ai.render(s.ai)
    rc = ai.session(ai_command(s, d, env), cwd=d / "repo", env=os.environ | env, render=render, log=log)
    result = ai.result(s.ai, log.with_suffix(".jsonl").read_text().splitlines())
    if result is None:
        raise RuntimeError(f"{s.ai.name} ended with {rc}, without a result: {log}")
    (d / "result.json").write_text(json.dumps(result) + "\n")


def app_jar(repo: Path) -> Path:
    """The runnable jar Maven built, not the ones it writes alongside it."""
    jars = [j for j in (repo / "target").glob("*.jar") if not any(x in j.name for x in _JAR_EXCLUDED)]
    if not jars:
        raise RuntimeError("no jar in target/ after the build")
    return max(jars, key=lambda j: j.stat().st_mtime)


def build(s: Settings, rid: str) -> Path:
    """mvn package on the clone, the tests included; the runnable jar."""
    d = run_dir(s, rid)
    try:
        run(["mvn", "-q", "-B", "package"], cwd=d / "repo", env=tools_env(s), log=d / "logs/build.log")
    except RuntimeError as err:
        raise RuntimeError(f"the build fails with the change:\n{err}") from None
    return app_jar(d / "repo")


def pg_init(repo: Path) -> None:
    """`local/postgres-init/*.sql` into the database of DATABASE_URL (this step's env), in the order of their
    names, each in one transaction, as the postgres image's /docker-entrypoint-initdb.d does."""
    for f in sorted((repo / "local/postgres-init").glob("*.sql")):
        console.log(f"loading local/postgres-init/{f.name}")
        run(["psql", os.environ["DATABASE_URL"], "-X", "-q", "-v", "ON_ERROR_STOP=1", "--single-transaction",
             "-f", str(f)])  # fmt: skip


def feature_check(s: Settings, rid: str, url: str) -> str:
    """The AI's feature check against the sandbox, with nothing but PATH and HOME: no more than the AI could."""
    d = run_dir(s, rid)
    return run(["env", "-i", f"PATH={os.environ['PATH']}", f"HOME={d / 'work'}", "sh", str(d / CHECK), url])


# ---------------------------------------------------------------------- the agent computer's setup


def setup(s: Settings) -> None:
    """git, jq, psql and python3; the JDK and Maven (lib.sh's install_java, as on the app's computer); the AI
    (`core.agent.ai`) for its own Unix user, which can use the JDK, Maven and tools here, but not this user's
    files."""
    d, a = HOME / s.agent.dir, s.app
    for sub in ("bin", "tools", "runs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    lib("apt_install git jq curl postgresql-client python3")
    lib(f"install_java {q(d, str(a.jdk), a.maven)}")
    try:
        pwd.getpwnam(AI_USER)
    except KeyError:
        console.log(f"creating user {AI_USER}")
        run(["sudo", "-n", "useradd", "-m", "-s", "/bin/bash", AI_USER])
    run(["chmod", "o+x", str(HOME), str(d), str(d / "runs")])
    run(["chmod", "-R", "o+rX", str(d / "jdk"), str(d / "maven"), str(d / "tools")])
    lend_nevia(s)
    console.log(f"agent ready: {s.ai.name} {ai.install(s.ai, s.agent.computer, AI_USER)} as {AI_USER}")


def lend_nevia(s: Settings) -> None:
    """The nevia CLI for the AI's user too, signed in with a copy of the agent's key file (mode 600, its own):
    the AI drives Nevia as the agent does. Again at each setup, so that a new key reaches it."""
    home = Path(pwd.getpwnam(AI_USER).pw_dir)
    key = home / s.agent.key_path
    lent = ((HOME / ai.BIN / "nevia", home / ai.BIN / "nevia", "755"), (HOME / s.agent.key_path, key, "600"))
    # Its directories made, then all of ~/.config and ~/.local its own: the CLI writes its config there.
    run(["sudo", "-n", "mkdir", "-p", *(str(dest.parent) for _, dest, _ in lent)])
    run(["sudo", "-n", "chown", "-R", f"{AI_USER}:{AI_USER}", str(home / ".config"), str(home / ".local")])
    for src, dest, mode in lent:
        run(["sudo", "-n", "install", "-o", AI_USER, "-g", AI_USER, "-m", mode, str(src), str(dest)])
    nv = f"~/{ai.BIN}/nevia"
    signs_in = f"{nv} auth use-key-file {q(key)} >/dev/null && {nv} workspace use {q(s.nevia.workspace)} >/dev/null"
    run(["sudo", "-n", "-u", AI_USER, "-H", "bash", "-lc", signs_in])
