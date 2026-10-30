from core.activity import Activity, events
from core.computer import carried


def test_events_keep_their_lines_and_the_ids_of_the_whole_log() -> None:
    lines = ["    lost: its event is before the tail", "10:00:01 $ ls", "    a", "10:00:02 failed: boom"]
    assert events(lines, "ai-1.log", start=40) == [
        Activity("ai-1.log:42", "10:00:01", "$ ls\n    a"),
        Activity("ai-1.log:44", "10:00:02", "failed: boom", failed=True),
    ]


def test_a_job_gets_your_time_zone_when_this_command_has_it(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Helsinki")
    assert carried({"A": "1"}) == {"A": "1", "TZ": "Europe/Helsinki"}
    monkeypatch.delenv("TZ")
    assert carried({"A": "1"}) == {"A": "1"}
