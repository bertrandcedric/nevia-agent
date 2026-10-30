from core.config import BUILD_DIR, RUN_ID, WORKSPACE_ENV, new_run_id, runs_dir
from m2p.config import Settings


def test_both_agents_make_and_keep_their_runs_alike() -> None:
    assert RUN_ID.fullmatch(new_run_id())
    assert not RUN_ID.fullmatch("2610291200001") and not RUN_ID.fullmatch("runs")
    assert (runs_dir("m2p"), runs_dir("acc")) == (BUILD_DIR / "m2p-runs", BUILD_DIR / "acc-runs")


def test_the_workspace_can_be_chosen_instead_of_agent_toml_s(monkeypatch) -> None:
    toml = Settings.load().nevia.workspace
    monkeypatch.setenv(WORKSPACE_ENV, "elsewhere")
    assert Settings.load().nevia.workspace == "elsewhere"
    monkeypatch.delenv(WORKSPACE_ENV)
    assert Settings.load().nevia.workspace == toml
