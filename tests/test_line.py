"""The line: an agent's question reaches the developer's voice, and the spoken answer goes back."""

import asyncio
import threading
import time
from pathlib import Path

import pytest

from dowel import decisions as files
from dowel import hook, line
from dowel.voice import live


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "loader.py").write_text(
        "def other():\n    return 1\n\n\ndef load_config(path):\n    if not path.exists():\n        return DEFAULTS\n    return read(path)\n")
    files.write(root, files.Record(uid="a1", kind="decision", created=1794700000.0, stance="keep", path="loader.py",
                                   symbol="load_config", title="A missing config file falls back to defaults.",
                                   reason="Staging hosts have no config file."))
    return root


def edit(repo: Path, old: str, new: str) -> dict | None:
    return hook.before_edit({"hook_event_name": "PreToolUse", "session_id": "s", "cwd": str(repo), "tool_name": "Edit",
                             "tool_input": {"file_path": str(repo / "loader.py"), "old_string": old, "new_string": new}})


def on_the_call(repo: Path, allow: bool, said: str) -> threading.Thread:
    """Stands in for the voice worker: keeps the line open and answers the first question it hears."""
    def run() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            line.beat(str(repo))
            for question in line.waiting(set()):
                line.answer(question["id"], allow, said)
                return
            time.sleep(0.05)
    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_with_nobody_on_the_call_the_edit_is_only_shown_the_decision(repo: Path) -> None:
    shown = edit(repo, "        return DEFAULTS", "        raise FileNotFoundError(path)")["hookSpecificOutput"]
    assert "permissionDecision" not in shown and "falls back to defaults" in shown["additionalContext"]


def test_a_spoken_refusal_stops_the_edit_and_carries_the_developers_words(repo: Path) -> None:
    line.beat(str(repo))
    answering = on_the_call(repo, allow=False, said="No, keep the fallback. Staging still has no config.")
    out = edit(repo, "        return DEFAULTS", "        raise FileNotFoundError(path)")["hookSpecificOutput"]
    answering.join()
    assert out["permissionDecision"] == "deny"
    assert "Staging still has no config." in out["permissionDecisionReason"]


def test_a_spoken_yes_lets_the_edit_through(repo: Path) -> None:
    line.beat(str(repo))
    answering = on_the_call(repo, allow=True, said="Yes, go ahead.")
    out = edit(repo, "        return DEFAULTS", "        return dict(DEFAULTS)")["hookSpecificOutput"]
    answering.join()
    assert out["permissionDecision"] == "allow" and "Yes, go ahead." in out["additionalContext"]


def test_an_edit_to_another_function_is_not_asked_about(repo: Path) -> None:
    line.beat(str(repo))
    out = edit(repo, "    return 1", "    return 2")["hookSpecificOutput"]
    assert "permissionDecision" not in out  # shown the decision, not held


def test_what_is_said_out_loud_is_short_and_carries_the_reason() -> None:
    said = line.spoken({"symbol": "load_config", "file": "loader.py", "decision": "A missing config file falls back to defaults.",
                        "reason": "Staging hosts have no config file."})
    assert said.startswith("Quick one from your agent.") and "because staging hosts have no config file" in said
    assert said.endswith("Allow the change, or refuse it?") and len(said) < 260


def test_yes_and_no_are_told_apart_and_a_muddle_is_asked_again() -> None:
    hear = lambda said: asyncio.run(live.allow_or_refuse(None, "keep the fallback", said))
    assert hear("yeah go ahead") is True and hear("no, leave it") is False
    assert hear("hmm, what does it change?") is None and hear("yes, no, wait") is None
    assert hear("haan kar do") is True and hear("nahi, rehne do") is False
