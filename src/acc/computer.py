"""acc's computers: its bundled scripts (the stack's and the agent computer's) and jobs."""

from dataclasses import dataclass
from typing import ClassVar

from core.computer import Computer as _Computer
from core.computer import JobFailed, redact, secret

__all__ = ["JOBS_DIR", "Computer", "JobFailed", "redact", "secret"]

PACKAGE = "acc"  # its scripts are in acc/scripts/
JOBS_DIR = "$HOME/.acc-jobs"


@dataclass
class Computer(_Computer):
    PACKAGE: ClassVar[str] = PACKAGE
    JOBS_DIR: ClassVar[str] = JOBS_DIR
