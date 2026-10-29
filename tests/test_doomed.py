from core.doomed import Doomed


def test_doomed_goes_through_json_adds_up_and_reads_as_lines() -> None:
    theirs = Doomed(["acc-app-1"], [("acc-app", "acc-1")], ["acc-pg-1"], ["ACC_DATABASE_URL_1"], dirs=["acc/runs/1"])
    mine = Doomed(["acc-agent"], secrets=["ACC_GITHUB_TOKEN"], principals=["p1"])
    assert Doomed.from_json("a log line\n" + theirs.to_json()) == theirs
    both = theirs + mine
    assert both.computers == ["acc-app-1", "acc-agent"] and both.secrets == ["ACC_DATABASE_URL_1", "ACC_GITHUB_TOKEN"]
    assert both.lines() == [
        "computers: acc-app-1, acc-agent", "checkpoints: acc-1 of acc-app", "databases: acc-pg-1",
        "secrets: ACC_DATABASE_URL_1, ACC_GITHUB_TOKEN", "principals: p1", "files: ~/acc/runs/1",
    ]  # fmt: skip
    assert not Doomed() and Doomed(dirs=["x"]) and not Doomed(dirs=["x"]).on_nevia
