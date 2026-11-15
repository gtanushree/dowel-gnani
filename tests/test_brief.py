"""The scope map and the reviewer's brief, built with no model."""

import json
import subprocess
from pathlib import Path

import pytest
from test_memory import LOADER, finding

from dowel import brief, check, scope, store
from dowel.memory import Memory
from dowel.store import ItemState


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / "repo"
    for folder in ("webhooks", "tests", "billing"):
        (root / folder).mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    (root / "loader.py").write_text(LOADER)
    (root / "webhooks/deliver.py").write_text("def deliver(event):\n    return send(event)\n")
    (root / "tests/test_deliver.py").write_text("def test_deliver():\n    assert deliver(1)\n")
    (root / "billing/invoice.py").write_text("def total(items):\n    return sum(items)\n")
    (root / "uv.lock").write_text("a\n")
    git(root, "add", "."), git(root, "commit", "-qm", "start")
    git(root, "switch", "-qc", "retry")
    (root / "webhooks/deliver.py").write_text("def deliver(event):\n    for _ in range(3):\n        send(event)\n")
    (root / "tests/test_deliver.py").write_text("def test_deliver():\n    assert deliver(1)\n    assert deliver(2)\n")
    (root / "loader.py").write_text(LOADER.replace("return read(path)", "return read(path) or DEFAULTS"))
    (root / "billing/invoice.py").write_text("def total(items):\n    return round(sum(items), 2)\n")
    (root / "uv.lock").write_text("b\n")
    return root


def base(repo: Path) -> str:
    return git(repo, "merge-base", "main", "HEAD").strip()


def labels(repo: Path, prompts: Path | None = None) -> dict[str, str]:
    return {e.path: e.label for e in scope.build(repo, base(repo), prompts).entries}


def test_with_nothing_said_about_what_was_asked_the_map_does_not_guess(repo: Path) -> None:
    got = scope.build(repo, base(repo))
    assert got.asked == "" and {e.label for e in got.entries} == {scope.UNEXPLAINED, scope.MECHANICAL}
    assert "nothing recorded" in got.of(scope.UNEXPLAINED)[0].why


def test_the_developers_prompt_sorts_asked_needed_mechanical_and_unexplained(repo: Path, tmp_path: Path) -> None:
    prompts = tmp_path / "prompts.jsonl"
    prompts.write_text(json.dumps({"at": 9e12, "prompt": "Add retries to webhook delivery in deliver.py"}) + "\n")
    assert labels(repo, prompts) == {
        "webhooks/deliver.py": scope.ASKED, "tests/test_deliver.py": scope.NEEDED, "uv.lock": scope.MECHANICAL,
        "loader.py": scope.UNEXPLAINED, "billing/invoice.py": scope.UNEXPLAINED,
    }
    got = scope.build(repo, base(repo), prompts)
    assert got.source == "your prompt" and got.entries[0].label == scope.UNEXPLAINED  # unexplained comes first


def test_commit_messages_are_used_when_no_prompt_was_recorded(repo: Path) -> None:
    git(repo, "add", "."), git(repo, "commit", "-qm", "Retry webhook delivery three times")
    got = scope.build(repo, base(repo))
    assert got.source == "commit messages"
    assert {e.path for e in got.of(scope.ASKED)} == {"webhooks/deliver.py"}


def test_the_brief_leads_with_the_unexplained_and_quotes_the_author(repo: Path) -> None:
    (store.runs_dir(str(repo.resolve())) / "prompts.jsonl").write_text(
        json.dumps({"at": 9e12, "prompt": "Add retries to webhook delivery in deliver.py"}) + "\n")
    Memory(repo).remember_answer(finding(), ItemState(answer="keep", note="Staging hosts have no config file."),
                                 given="spoken")
    (repo / "tests/test_deliver.py").write_text("def test_deliver():\n    assert True\n")
    text = brief.build(repo, base(repo), "main")

    assert text.index("## Not explained (2)") < text.index("## Decided by the author (1)") < text.index("## Still open")
    assert "`billing/invoice.py`" in text and "nothing in your prompt points to it" in text
    assert '**Keeping it.** "Staging hosts have no config file." (spoken)' in text
    assert "## Tests made weaker" in text and "**No reason recorded.**" in text
    assert "uv.lock" not in text.split("## Where to start")[1].split("\n\n")[0]  # mechanical files are folded away
    assert "1 other file left out" in text

    for p in check.gather(repo, base(repo)).problems:
        if p.kind == "test":
            check.accept(repo, p, "deliver is being replaced; its test goes next sprint")
    text = brief.build(repo, base(repo), "main")
    assert "No reason recorded" not in text and "deliver is being replaced" in text
