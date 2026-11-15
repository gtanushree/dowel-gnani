"""Things that must hold whatever the models do: what leaves the machine, and what a run may spend."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from dowel.llm import CostCapReached, Ledger
from dowel.tools import RepoTools


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "app.py").write_text("print('hi')\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "app.py"], check=True)
    (tmp_path / "notes-with-keys.txt").write_text("token = abc\n")  # never added to git
    return tmp_path


def test_untracked_files_cannot_be_read(tmp_path: Path) -> None:
    tools = RepoTools(_repo(tmp_path), "HEAD", 6000)
    read = lambda path: asyncio.run(tools.run("read_file", {"path": path, "start_line": 1, "end_line": 5}))
    assert "print('hi')" in read("app.py")
    with pytest.raises(ValueError, match="tracked"):
        read("notes-with-keys.txt")


def test_calls_in_flight_count_against_the_limit() -> None:
    ledger = Ledger(0.10)
    holds = [ledger.reserve("claude-sonnet-5-5") for _ in range(2)]  # two calls start side by side at 4 cents
    with pytest.raises(CostCapReached):
        ledger.reserve("claude-sonnet-5-5")  # a third would take the run past ten cents
    ledger.release(holds[0])
    ledger.reserve("claude-sonnet-5-5")  # room again once one finishes without spending


def test_one_expensive_call_does_not_stop_many_cheap_ones() -> None:
    """A review that spent 15 cents planning must still be able to run its cheap checks under a 50 cent limit."""
    from types import SimpleNamespace

    ledger = Ledger(0.50)
    hold = ledger.reserve("claude-sonnet-5-5")
    ledger.release(hold)
    ledger.add("claude-sonnet-5-5", SimpleNamespace(input_tokens=30_000, output_tokens=9_000))  # the planning call: 15 cents
    assert ledger.usage.cost_usd > 0.10
    for _ in range(12):
        ledger.reserve("claude-haiku-5-5")  # a dozen investigators start at once
