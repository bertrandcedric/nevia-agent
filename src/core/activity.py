"""What an AI agent did, read back from the lines its render.jq writes (common.jq's vocabulary): one event per
`HH:MM:SS kind: text` line, with the indented lines under it as part of it."""

import re
from dataclasses import dataclass

from core.computer import redact

EVENT = re.compile(r"^(\d\d:\d\d:\d\d) (.*)$")
BRIEF = ("intent:", "says:", "failed:", "error:")  # what a short view keeps: what it means to do, says, fails


@dataclass(frozen=True)
class Activity:
    id: str
    time: str  # HH:MM:SS, in the render's time zone
    text: str
    failed: bool = False

    @property
    def brief(self) -> bool:
        return self.text.startswith(BRIEF)


def event(id_: str, time: str, text: str) -> Activity:
    text = redact(text)
    return Activity(id_, time, text, text.startswith("failed:"))


def events(lines: list[str], prefix: str = "", start: int = 0) -> list[Activity]:
    """The events of a log's lines; a line under an event (indented) joins it, and one before any is left out.
    `prefix` makes the ids (`prefix:N`, N the event's line) apart from another log's; `start` is the log's lines
    before these (a tail's), so that N is the line in the whole log."""
    out: list[Activity] = []
    for n, line in enumerate(lines, start + 1):
        if m := EVENT.match(line):
            out.append(event(f"{prefix}:{n}", m[1], m[2]))
        elif out and line.startswith("    "):
            last = out[-1]
            out[-1] = event(last.id, last.time, f"{last.text}\n{line}")
    return out
