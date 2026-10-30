"""The SQL a run's final load ran, from its collected results (your machine): the DDL mongo2pg's import applied,
then the agent's data fix."""

import re
from pathlib import Path

FINAL_SQL = "final.sql"  # next to DONE.md in the collected results
APPLIED = re.compile(r"Created PostgreSQL objects from (\S+\.sql)")  # mongo2pg import's line for each DDL file


def applied_ddl(project: Path) -> list[Path]:
    """The DDL files mongo2pg's import ran, in order, from the project's logs; all of `schema/` if no log
    says. The paths in the logs are the agent computer's: only their part under the project counts."""
    applied = []
    for log in sorted(project.rglob("*.log")):
        for m in APPLIED.finditer(log.read_text(errors="replace")):
            rel = m[1].split(f"/{project.name}/", 1)[-1]
            if (f := project / rel).is_file() and f not in applied:
                applied.append(f)
    return applied or sorted((project / "schema").rglob("*.sql"))


def final_sql(project: Path, target: str) -> str:
    """The SQL the final load ran, in order: the DDL mongo2pg's import applied, then the data fix the agent
    ran after the import, if any. Empty when the project has no DDL."""
    ddl = applied_ddl(project) if project.is_dir() else []
    if not ddl:
        return ""
    parts = [f"-- The SQL {target} was loaded with from tries/{project.name}/, in the order it ran.\n"]
    parts += [f"\n-- {p.relative_to(project)}, applied by mongo2pg import\n{p.read_text()}" for p in ddl]
    if (fix := project / "normalize.sql").is_file():
        parts.append(f"\n-- normalize.sql, run after the import\n{fix.read_text()}")
    return "".join(parts)
