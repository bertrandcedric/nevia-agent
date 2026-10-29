import pytest

from acc import pr


def test_proposal_takes_the_title_and_body_tags() -> None:
    result = {"result": "Done.\n<title>## Migrate   to Postgres</title>\n<body>## Solution\nTables.</body>"}
    assert pr.proposal(result) == ("Migrate to Postgres", "## Solution\nTables.")


def test_proposal_without_tags_is_the_first_line_and_the_rest() -> None:
    assert pr.proposal({"result": "Add totals\n\nThe sum of the orders."}) == ("Add totals", "The sum of the orders.")


def test_proposal_of_a_failed_claude_says_why() -> None:
    with pytest.raises(pr.NoProposal, match=r"error_max_turns after 150 turns \(raise claude.max_turns\)"):
        pr.proposal({"is_error": True, "subtype": "error_max_turns", "num_turns": 150})


def test_body_has_the_analysis_the_feature_check_and_the_preview() -> None:
    v = pr.Validation(
        stack="acc-app", base_branch="main", sandbox="acc-app-261029120000", checkpoint="acc-261029120000",
        kind="mongo", database="a fork", url="https://sb.example", analysis="| Collection |",
        feature_check="curl ...", feature_log="\x1b[1mok\x1b[0m total",
    )  # fmt: skip
    text = pr.body("## Task\nTotals.", v, "261029120000")
    assert text.startswith("## Task\nTotals.")
    assert "## Data analysis (mongo2pg)" in text
    assert "```sh\ncurl ...\n```" in text
    assert "ok total" in text and "\x1b" not in text
    assert "acc clean --run 261029120000" in text
    assert text.endswith(pr.FOOTER)


def test_report_has_the_steps_the_cost_and_what_is_left() -> None:
    r = pr.Report(
        run="261029120000", kind="migrate", repo="o/r", base_branch="main", started="2026-10-29 12:00 UTC",
        outcome="ok", steps=[("clone", 30), ("claude", 1200.4)], pr_url="https://github.com/o/r/pull/1",
        result={"total_cost_usd": 1.23456, "num_turns": 40, "modelUsage": {"m": {"inputTokens": 5, "costUSD": 1.2}}},
        left=["computer `acc-app-261029120000`"],
    )  # fmt: skip
    text = pr.report(r)
    assert "| **total** | **20m30s** |" in text
    assert "cost: **$1.2346**" in text
    assert "| `m` | 5 | 0 | 0 | 0 | $1.2000 |" in text
    assert "- computer `acc-app-261029120000`" in text
