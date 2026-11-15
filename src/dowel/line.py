"""The line between a coding agent and the developer's voice.

When a walkthrough is open, an agent that is about to change code under a decision does not get
a silent yes or a text prompt nobody is watching. The hook puts the question on the line, Dowel
asks it out loud, and the developer's spoken answer goes back to the agent as allow or refuse,
in their words.

Two processes that never import each other meet here: the hook, which must stay tiny and fast,
and the voice worker. They share a folder. The worker keeps a heartbeat file fresh while someone
is on the call; the hook writes a question file and waits for an answer file beside it. No
socket, no server, nothing to configure. If nobody is on the line the hook finds that out in a
millisecond and behaves as it always did.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

OPEN_FOR_S = 6  # a heartbeat older than this means nobody is on the call
WAIT_S = 90  # how long an agent waits for a spoken answer before falling back to its own prompt


def _root() -> Path:
    path = Path.home() / ".dowel" / "line"
    (path / "holds").mkdir(parents=True, exist_ok=True)
    return path


def beat(repo: str) -> None:
    """Called by the voice worker every second or two while a call is live."""
    (_root() / "open").write_text(json.dumps({"at": time.time(), "repo": repo}))


def hang_up() -> None:
    (_root() / "open").unlink(missing_ok=True)


def is_open(repo: str | None = None) -> bool:
    file = Path.home() / ".dowel" / "line" / "open"
    try:
        state = json.loads(file.read_text())
    except (OSError, ValueError):
        return False
    fresh = time.time() - float(state.get("at", 0)) < OPEN_FOR_S
    return fresh and (repo is None or state.get("repo") == repo)


def ask(question: dict, wait_s: float = WAIT_S, poll_s: float = 0.25) -> dict | None:
    """Put a question on the line and wait for the answer. None if nobody answered in time."""
    ident = f"{int(time.time() * 1000):x}{secrets.token_hex(2)}"
    holds = _root() / "holds"
    asked, answered = holds / f"{ident}.json", holds / f"{ident}.answer.json"
    asked.write_text(json.dumps({"id": ident, "at": time.time(), **question}))
    try:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if answered.exists():
                try:
                    return json.loads(answered.read_text())
                except ValueError:
                    return None
            if not is_open():  # the call ended while the agent was waiting
                return None
            time.sleep(poll_s)
        return None
    finally:
        asked.unlink(missing_ok=True)
        answered.unlink(missing_ok=True)


def waiting(seen: set[str]) -> list[dict]:
    """Questions on the line that the worker has not picked up yet, oldest first."""
    out = []
    for file in sorted((_root() / "holds").glob("*.json")):
        if file.name.endswith(".answer.json") or file.stem in seen:
            continue
        try:
            out.append(json.loads(file.read_text()))
        except ValueError:
            continue
    return out


def answer(ident: str, allow: bool, said: str) -> None:
    (_root() / "holds" / f"{ident}.answer.json").write_text(json.dumps({"allow": allow, "said": said[:400]}))


def spoken(question: dict) -> str:
    """What Dowel says out loud. Short: the developer may be in the middle of something else."""
    why = f" because {question['reason'].rstrip('.').lower()[:1] + question['reason'].rstrip('.')[1:]}" if question.get("reason") else ""
    where = f"{question.get('symbol') or question.get('file')}"
    return (f"Quick one from your agent. It wants to change {where.replace('_', ' ')}. "
            f"You decided to keep this: {question['decision'].rstrip('.')}{why}. Allow the change, or refuse it?")
