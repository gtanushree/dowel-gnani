"""A guard test is kept only if it passes now and fails when the decision is undone."""

import asyncio
from pathlib import Path

import pytest
from test_memory import finding

from dowel import guard
from dowel.memory import Memory
from dowel.store import ItemState

LOADER = '''DEFAULTS = {"retries": 3}


def load_config(path):
    if not path.exists():
        return DEFAULTS
    return {"retries": int(path.read_text())}
'''
GOOD_TEST = '''from loader import DEFAULTS, load_config


def test_missing_file_falls_back(tmp_path):
    assert load_config(tmp_path / "none.toml") == DEFAULTS, "A missing config file now falls back to defaults."
'''
REVERSED = '''def load_config(path):
    if not path.exists():
        raise FileNotFoundError(path)
    return {"retries": int(path.read_text())}
'''


class FakeClaude:
    def __init__(self, **draft) -> None:
        self._draft = {"can_be_tested": True, "why_not": "", "test_code": GOOD_TEST, "reversed_function": REVERSED,
                       **draft}

    async def structured(self, **_kwargs) -> guard.Draft:
        return guard.Draft(**self._draft)


@pytest.fixture
def decided(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "loader.py").write_text(LOADER)
    memory = Memory(repo)
    memory.remember_answer(finding(line=5), ItemState(answer="keep", note="Staging hosts have no config file."))
    (decision,) = memory.decisions()
    return repo, decision, memory.locate(decision)


def attempt(decided, **draft) -> guard.Outcome:
    repo, decision, where = decided
    return asyncio.run(guard.write(repo, decision, where, FakeClaude(**draft), "model"))


def test_a_test_that_passes_now_and_fails_when_reversed_is_kept(decided) -> None:
    out = attempt(decided)
    text = (decided[0] / out.written).read_text()
    assert out.written.startswith(".dowel/guards/test_load_config_")
    assert "Do not edit: retire the decision instead." in text and "Staging hosts have no config file." in text


def test_a_test_that_would_pass_either_way_is_thrown_away(decided) -> None:
    out = attempt(decided, test_code="from loader import load_config\n\n\ndef test_exists():\n    assert callable(load_config)\n")
    assert "guards nothing" in out.refused and not (decided[0] / guard.FOLDER).exists()


def test_a_test_that_fails_on_todays_code_is_thrown_away(decided) -> None:
    out = attempt(decided, test_code="def test_wrong():\n    assert 1 == 2, 'no'\n")
    assert "does not pass on the code as it is now" in out.refused and not (decided[0] / guard.FOLDER).exists()


def test_a_decision_that_cannot_be_a_test_is_reported_and_nothing_is_written(decided) -> None:
    out = attempt(decided, can_be_tested=False, why_not="This is about which library to use.", test_code="")
    assert out.refused == "This is about which library to use." and not (decided[0] / guard.FOLDER).exists()


def test_a_reversed_function_that_does_not_even_load_proves_nothing(decided) -> None:
    out = attempt(decided, reversed_function="def load_config(path)\n    return 1\n")  # a syntax error
    assert "could not be run" in out.refused and not (decided[0] / guard.FOLDER).exists()
    out = attempt(decided, reversed_function="  ")
    assert "no reversed version" in out.refused
