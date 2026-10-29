import os

import pytest

from core.local import env


def test_parse_reads_key_values_quotes_and_export() -> None:
    text = "# a comment\n\nAIVEN_AI_API_KEY=k1\nexport GITHUB_TOKEN='github_pat_x'\nEMPTY=\nQ=\"a b\"\n"
    assert env.parse(text) == {"AIVEN_AI_API_KEY": "k1", "GITHUB_TOKEN": "github_pat_x", "EMPTY": "", "Q": "a b"}


def test_a_bad_line_names_its_key_never_its_value() -> None:
    with pytest.raises(ValueError, match="'not a line'"):
        env.parse("not a line")
    with pytest.raises(ValueError) as err:
        env.parse("BAD-KEY=s3cr3t")
    assert "s3cr3t" not in str(err.value)


def test_load_keeps_what_the_shell_has(tmp_path, monkeypatch) -> None:
    f = tmp_path / ".env"
    f.write_text("ACC_TEST_A=from-file\nACC_TEST_B=from-file\nACC_TEST_C=\n")
    monkeypatch.setenv("ACC_TEST_A", "from-shell")
    monkeypatch.delenv("ACC_TEST_B", raising=False)
    monkeypatch.delenv("ACC_TEST_C", raising=False)
    try:
        assert env.load(f) == ["ACC_TEST_B"]
        assert os.environ["ACC_TEST_A"] == "from-shell"
        assert os.environ["ACC_TEST_B"] == "from-file"
        assert "ACC_TEST_C" not in os.environ
    finally:
        os.environ.pop("ACC_TEST_B", None)


def test_no_file_is_nothing(tmp_path) -> None:
    assert env.load(tmp_path / ".env") == []


def test_the_example_lists_what_the_agents_read() -> None:
    example = env.parse((env.ROOT / ".env.example").read_text())
    assert {"AIVEN_AI_API_KEY", "GITHUB_TOKEN"} <= set(example)
    assert not any(example.values())  # no value in the example
