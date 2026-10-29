from pathlib import Path

import pytest

from m2p import usage
from m2p.config import Settings


def test_requests_are_summed_by_model_and_the_totals_dont_count_twice(tmp_path: Path) -> None:
    log = tmp_path / "session-1.log"
    log.write_text(
        "19:21:31 usage: model=m input=1000 cached=800 output=50\n"
        "19:21:31 says: something in plain text\n"
        "19:21:32 usage: model= input=500 cached=0 output=20\n"
        "usage total: model=m requests=2 input=1500 cached=800 output=70\n"  # the requests above, again
    )
    assert usage.session(log, "default") == {"m": usage.Usage(1, 1000, 800, 50), "default": usage.Usage(1, 500, 0, 20)}


def test_sessions_in_order_and_their_total(tmp_path: Path) -> None:
    for n in (10, 2):
        (tmp_path / f"session-{n}.log").write_text(f"usage: model=m input={n} cached=0 output=1\n")
    sessions = usage.run(tmp_path, "m")
    assert list(sessions) == ["session-2", "session-10"]
    assert usage.total(sessions) == {"m": usage.Usage(2, 12, 0, 2)}


def test_cached_tokens_are_priced_apart(settings: Settings) -> None:
    prices = settings.copilot.model_copy(update={"price_input": 2.0, "price_cached_input": 0.5, "price_output": 8.0})
    u = usage.Usage(requests=1, input=1_000_000, cached=600_000, output=100_000)
    assert u.cost(prices) == pytest.approx(0.4 * 2.0 + 0.6 * 0.5 + 0.1 * 8.0)


def test_the_rendered_usage_lines_are_read(tmp_path: Path) -> None:
    log = tmp_path / "session-1.log"
    log.write_text(
        "2026-10-28T19:21:29Z session 1 started\n"
        "19:21:31 usage: model=m input=1000 cached=800 output=50\n"
        "19:21:32 $ echo usage: model=x input=9\n"  # a command is not a usage line
        "usage total: model=m requests=1 input=1000 cached=800 output=50\n"
    )
    assert usage.session(log, "default") == {"m": usage.Usage(1, 1000, 800, 50)}
    log.write_text("usage total: model=m requests=3 input=900 cached=100 output=9\n")
    assert usage.session(log, "default") == {"m": usage.Usage(3, 900, 100, 9)}
