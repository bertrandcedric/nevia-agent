import re

import pytest
from typer.testing import CliRunner

from core.local import cli


def _main(monkeypatch, err: Exception) -> None:
    def app() -> None:
        raise err

    monkeypatch.setattr(cli.env, "load", lambda: None)
    monkeypatch.setattr(cli, "app", app)
    cli.main()


def test_an_error_raised_on_purpose_prints_as_one_line(monkeypatch, capsys) -> None:
    with pytest.raises(SystemExit) as done:
        _main(monkeypatch, RuntimeError("no computer m2p-agent"))
    assert done.value.code == 1
    assert "error: no computer m2p-agent" in capsys.readouterr().out


def test_a_bug_keeps_its_traceback(monkeypatch) -> None:
    with pytest.raises(KeyError):
        _main(monkeypatch, KeyError("total_cost_usd"))


def test_cli_lists_its_commands() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert set(re.findall(r"^│ ([a-z]\S*) ", result.output, re.M)) == {"m2p", "acc"}
    for agent in ("m2p", "acc"):
        listed = set(re.findall(r"^│ ([a-z]\S*) ", CliRunner().invoke(cli.app, [agent, "--help"]).output, re.M))
        assert listed == {"setup", "run", "status", "stop", "clean", "destroy"}
