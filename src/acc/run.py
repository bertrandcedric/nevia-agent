"""A run as both sides know it: its kinds and steps, its state (build/acc-runs/<run>/state.json on the agent
computer, collected to the same place on your machine), its orchestrator's job, and the options of the commands
that start it, here and there."""

from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import BaseModel

from core.config import RUN_ID, runs_dir

RunKind = Literal["migrate", "task"]
Kind = Literal["mongo", "postgres"]  # the database the app is on: its application.properties' database.kind
RUNS_DIR = runs_dir("acc")
STEPS: dict[RunKind, tuple[str, ...]] = {
    "migrate": ("clone", "stack", "analysis", "ai", "build", "sandbox", "pr"),
    "task": ("clone", "stack", "ai", "build", "sandbox", "feature-check", "pr"),
}
RUN_JOB = "acc-run"  # a run's orchestrator, on the agent computer


class RunState(BaseModel):
    """A run as `run` left it; its steps are done in order, each once."""

    id: str
    kind: RunKind
    task: str = ""
    started: str
    done: list[str] = []
    durations: dict[str, float] = {}
    outcome: str = ""  # empty while it goes, then ok, no change, stopped or failed: <why>
    stack_kind: Kind | Literal[""] = ""
    new_kind: Kind | Literal[""] = ""
    analysis: str = ""
    title: str = ""
    text: str = ""
    jar: str = ""
    sandbox_url: str = ""
    database: str = ""
    feature_log: str = ""
    pr_url: str = ""

    @property
    def path(self) -> Path:
        return RUNS_DIR / self.id

    @property
    def finished(self) -> bool:
        return self.outcome in {"ok", "no change"}

    def save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "state.json").write_text(self.model_dump_json(indent=2) + "\n")

    @classmethod
    def load(cls, run: str) -> RunState:
        f = RUNS_DIR / run / "state.json"
        if not f.is_file():
            raise FileNotFoundError(f"no run {run} in {RUNS_DIR}")
        return cls.model_validate_json(f.read_text())

    @classmethod
    def latest(cls) -> RunState | None:
        runs = sorted(p.name for p in RUNS_DIR.glob("*") if RUN_ID.fullmatch(p.name) and (p / "state.json").is_file())
        return cls.load(runs[-1]) if runs else None


def run_dir_of(agent_dir: str, run: str) -> str:
    """The run's directory on the agent computer, under its home: its clone, the AI's files, the analysis."""
    return f"{agent_dir}/runs/{run}"


YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Don't ask first.")]
Mode = Literal["migrate", "task", "resume"]
ModeArg = Annotated[
    Mode, typer.Argument(help="migrate, task, or resume: the last run taken up (or followed, if it is going).")
]
FromOpt = Annotated[
    str | None, typer.Option("--from", help="With resume: redo this step and the ones after (e.g. sandbox).")
]
