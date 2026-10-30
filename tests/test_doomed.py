from nevia_kit.testing import FakeApi

from core.doomed import Doomed, delete
from core.nevia import Nevia


def test_doomed_goes_through_json_adds_up_and_reads_as_lines() -> None:
    theirs = Doomed(["acc-app-1"], [("acc-app", "acc-1")], ["acc-pg-1"], ["ACC_DATABASE_URL_1"], dirs=["acc/runs/1"])
    mine = Doomed(["acc-agent"], secrets=["ACC_GITHUB_TOKEN"], principals=["p1"])
    assert Doomed.from_json(theirs.to_json()) == theirs
    assert Doomed.from_json('{"jobs": "*-1.*", "computers": ["acc-app-1"]}') == Doomed(["acc-app-1"], jobs="*-1.*")
    both = theirs + mine
    assert both.computers == ["acc-app-1", "acc-agent"] and both.secrets == ["ACC_DATABASE_URL_1", "ACC_GITHUB_TOKEN"]
    assert both.lines() == [
        "computers: acc-app-1, acc-agent", "checkpoints: acc-1 of acc-app", "databases: acc-pg-1",
        "secrets: ACC_DATABASE_URL_1, ACC_GITHUB_TOKEN", "principals: p1", "files: ~/acc/runs/1",
    ]  # fmt: skip
    assert not Doomed() and Doomed(dirs=["x"]) and not Doomed(dirs=["x"]).on_nevia


def test_delete_takes_a_run_s_computers_then_its_checkpoints_files_and_the_rest(fake: FakeApi, nevia: Nevia) -> None:
    app = fake.add("computers", "acc-app")
    sandbox = fake.add("computers", "acc-app-261029120000")
    fake.add("computers", "acc-mongo")
    fake.add("checkpoints", "acc-261029120000", source=app["id"])
    fake.add("databases", "acc-pg")
    nevia.secrets.set("ACC_X", "x")
    files: list[tuple[list[str], int]] = []  # the dirs, after how many deletions
    d = Doomed(["acc-mongo", "acc-app-261029120000"], [("acc-app", "acc-261029120000")], ["acc-pg"], ["ACC_X"],
               dirs=["acc/runs"])  # fmt: skip
    delete(nevia, d, files=lambda d: files.append((d.dirs, len(_deletions(fake)))))
    assert files == [(["acc/runs"], 2)]
    assert _deletions(fake) == ["deleteComputer", "deleteComputerCheckpoint", "deleteComputer", "deleteDatabase",
                                "deleteSecret"]  # fmt: skip
    assert sandbox["id"] in fake.ops("deleteComputer")[0].path  # the run's first, listed second
    assert not fake.find("computers", "acc-mongo") and fake.find("computers", "acc-app")


def _deletions(fake: FakeApi) -> list[str]:
    return [c.op for c in fake.calls if c.op.startswith("delete")]
