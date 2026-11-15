"""Tests for the deterministic parts of the engine (no model calls)."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from dowel import context as context_pack
from dowel.gitops import changed_lines, load_change
from dowel.tools import RepoTools


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "loader.py").write_text("def load(path):\n    return open(path).read()\n")
    (tmp_path / "app.py").write_text("from loader import load\n\nprint(load('x'))\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    _git(tmp_path, "add", "loader.py", "app.py")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "switch", "-q", "-c", "feature")
    (tmp_path / "loader.py").write_text("def load(path):\n    if not path:\n        return ''\n    return open(path).read()\n")
    _git(tmp_path, "commit", "-q", "-am", "Return empty for a missing path")
    return tmp_path


def test_changed_lines_reads_new_file_ranges() -> None:
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,4 @@ def f():\n+a\n+b\n--- a/y.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
    assert changed_lines(diff) == {"x.py": [(1, 4)]}


def test_change_and_context_pack_find_callers(repo: Path) -> None:
    change = asyncio.run(load_change(repo, "main"))
    assert change.files == ["loader.py"]
    assert "Return empty for a missing path" in change.description
    pack = asyncio.run(context_pack.build(repo, change))
    assert pack.symbols == {"loader.py": ["load"]}
    assert any(use.startswith("app.py:") for use in pack.callers["load"])


def test_tools_refuse_secrets_and_paths_outside_the_repo(repo: Path) -> None:
    tools = RepoTools(repo, "main", max_chars=4000)
    read = {"start_line": 1, "end_line": 5}
    with pytest.raises(ValueError):
        asyncio.run(tools.run("read_file", {"path": ".env", **read}))
    with pytest.raises(ValueError):
        asyncio.run(tools.run("read_file", {"path": "../outside.txt", **read}))
    assert "if not path" in asyncio.run(tools.run("read_file", {"path": "loader.py", **read}))
    assert "if not path" not in asyncio.run(tools.run("old_version", {"path": "loader.py", **read}))
    assert "SECRET" not in asyncio.run(tools.run("grep", {"pattern": "SECRET", "path": "."}))
