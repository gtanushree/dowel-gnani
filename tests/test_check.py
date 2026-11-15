"""`dowel check`: what stops a change until a person has looked."""

import subprocess
from pathlib import Path

import pytest
from test_memory import LOADER, finding

from dowel import check
from dowel.memory import Memory
from dowel.store import ItemState


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")  # the index for this test lives here
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    (root / "loader.py").write_text(LOADER)
    (root / "tests/test_loader.py").write_text("def test_defaults():\n    assert load_config(None) == DEFAULTS\n")
    Memory(root).remember_answer(finding(), ItemState(answer="keep", note="staging has no config"))
    git(root, "add", "."), git(root, "commit", "-qm", "start")
    git(root, "switch", "-qc", "work")
    return root


def problems(repo: Path) -> list[tuple[str, str]]:
    return [(p.kind, p.where) for p in check.gather(repo, git(repo, "merge-base", "main", "HEAD").strip()).problems]


def test_a_change_that_touches_nothing_decided_passes(repo: Path) -> None:
    (repo / "loader.py").write_text(LOADER.replace("return 1", "return 2"))  # `other`, not the decided function
    assert problems(repo) == []


def test_changing_code_under_a_decision_needs_someone_to_look(repo: Path) -> None:
    (repo / "loader.py").write_text(LOADER.replace("return DEFAULTS", "raise ConfigMissing(path)"))
    assert problems(repo) == [("decision", "loader.py · load_config")]

    Memory(repo).mark_checked(1, Memory(repo).locate(Memory(repo).decisions()[0]).fingerprint)
    assert problems(repo) == []  # once this version has been judged fine, it is not raised again


def test_a_weakened_test_fails_until_a_reason_is_recorded(repo: Path) -> None:
    (repo / "tests/test_loader.py").write_text("def test_defaults():\n    assert True\n")
    base = git(repo, "merge-base", "main", "HEAD").strip()
    found = check.gather(repo, base).problems
    assert [p.kind for p in found] == ["test", "test"]

    with pytest.raises(ValueError, match="reason is required"):
        check.accept(repo, found[0], "  ")
    for p in found:
        check.accept(repo, p, "load_config is being removed next week; this test goes with it")
    assert problems(repo) == []

    (repo / "tests/test_loader.py").write_text("import pytest\n\n@pytest.mark.skip\ndef test_defaults():\n    assert True\n")
    assert [p.what for p in check.gather(repo, base).problems if p.kind == "test"] == ["1 test marked to be skipped"]


def test_only_a_weakened_test_can_be_accepted(repo: Path) -> None:
    (repo / "loader.py").write_text(LOADER.replace("return DEFAULTS", "raise ConfigMissing(path)"))
    (found,) = check.gather(repo, git(repo, "merge-base", "main", "HEAD").strip()).problems
    with pytest.raises(ValueError, match="retire"):
        check.accept(repo, found, "because")
