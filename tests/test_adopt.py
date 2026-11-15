"""Decisions proposed from a repository's own history."""

import subprocess
from pathlib import Path

import pytest

from dowel import adopt
from dowel.memory import Memory, record_stated


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "pay.py").write_text("def charge(order):\n    return send(order)\n\n\ndef refund(order):\n    return undo(order)\n")
    git(root, "add", "."), git(root, "commit", "-qm", "start")
    (root / "pay.py").write_text("def charge(order):\n    if order.id in already_charged:\n        return already_charged[order.id]\n"
                                 "    return send(order)\n\n\ndef refund(order):\n    return undo(order)\n")
    git(root, "commit", "-qam", "fix: never charge the same order twice")
    (root / "pay.py").write_text((root / "pay.py").read_text().replace("return undo(order)", "return undo(order)  # ok"))
    git(root, "commit", "-qam", "fix typo in comment")
    return root


def test_a_fix_still_in_the_code_is_proposed_as_a_decision_to_keep(repo: Path) -> None:
    (only,) = adopt.find(repo)
    assert (only.file, only.symbol, only.title) == ("pay.py", "charge", "Never charge the same order twice.")
    assert "taken from the history" in only.reason


def test_what_is_already_decided_is_not_proposed_again(repo: Path) -> None:
    (only,) = adopt.find(repo)
    record_stated(repo, only.file, only.line, only.title, "keep", only.reason, "from history")
    assert adopt.find(repo) == []
    (decision,) = Memory(repo).decisions()
    assert (decision.symbol, decision.stance, decision.given) == ("charge", "keep", "from history")


def test_a_fix_that_was_later_removed_is_not_proposed(repo: Path) -> None:
    (repo / "pay.py").write_text("def charge(order):\n    return send(order)\n\n\ndef refund(order):\n    return undo(order)\n")
    git(repo, "commit", "-qam", "simplify")
    assert adopt.find(repo) == []
