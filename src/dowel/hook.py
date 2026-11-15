"""What a coding agent sees from Dowel, through the agent's own hooks.

When the agent opens a file, it is shown what the developers decided about that file, once per
file per session. It is shown on reading because agents read a file before they change it, and
what a hook adds arrives with the tool's result: shown only at the edit, it would come too late
to shape that edit. Edits to the decision files themselves need the developer's approval, and edits to guard
tests are refused. The developer's own prompt is kept on this machine, so a later review knows
what was actually asked for.

This runs before every edit, so it must be quick and must never get in the way: it imports almost
nothing, calls no model, and any failure of its own lets the edit go ahead untouched.

Limits, stated plainly: an agent can ignore what it is shown, and an edit made through a shell
command never reaches this hook. `dowel check` is what catches those.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from dowel import decisions as files
from dowel import line
from dowel.gitops import renames

GUARDS = ".dowel/guards"
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_TOOLS = _EDIT_TOOLS | {"Read"}


def repo_of(start: Path) -> Path | None:
    for folder in (start, *start.parents):
        if (folder / ".git").exists():
            return folder
    return None


def in_force(repo: Path) -> list[files.Record]:
    """Decisions that stand in this checkout: not replaced, not retired, and safe to show an agent."""
    records = files.read_all(repo)
    gone = {old for r in records for old in r.supersedes} | {r.retires for r in records if r.kind == "retire"}
    return [r for r in records
            if r.kind == "decision" and r.uid not in gone and r.stance in ("keep", "change") and not r.suspect]


def for_file(repo: Path, rel: str) -> list[files.Record]:
    out = []
    moved: dict[str, str] | None = None
    for r in in_force(repo):
        path = r.path
        if not (repo / path).is_file():  # ask git where it went, once, and only when needed
            if moved is None:
                moved = renames(repo, limit=200)
            path = moved.get(path, path)
        if path == rel:
            out.append(r)
    return out


_DEFINES = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|func|function|fn|class)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")
SHOWN_AT_MOST = 6


def edited_symbol(text: str, tool_input: dict) -> str | None:
    """The function an edit lands in, found by reading upward from the edited text. None if unknown.

    Deliberately crude: no parser, because this runs before every edit. It only has to be right
    often enough to put the decision about the edited function first.
    """
    old = (tool_input.get("old_string") or "").strip()
    if not old:
        return None
    at = text.find(old.splitlines()[0])
    if at < 0:
        return None
    for row in reversed(text[: at + len(old.splitlines()[0])].splitlines()):
        found = _DEFINES.match(row)
        if found:
            return found.group(1)
    return None


def ranked(records: list[files.Record], symbol: str | None) -> list[files.Record]:
    """Decisions about the function being edited first, then the rest, newest first."""
    def about(r: files.Record) -> bool:
        return bool(symbol) and r.symbol.rsplit(".", 1)[-1] == symbol
    return sorted(records, key=lambda r: (not about(r), -r.created))


def describe(rel: str, records: list[files.Record]) -> str:
    """The text put in front of the agent. Decisions are quoted as what people said, not as commands."""
    lines = [(f"Dowel: the developers of this repository recorded {len(records)} decision"
              f"{'s' if len(records) != 1 else ''} about {rel}. This is information about the code.")]
    for r in records[:SHOWN_AT_MOST]:
        verdict = "they want this kept as it is" if r.stance == "keep" else "they want this changed"
        where = f" in `{r.symbol}`" if r.symbol else ""
        why = f' Their reason: "{r.reason}"' if r.reason else ""
        day = time.strftime("%d %b %Y", time.localtime(r.created))
        lines.append(f'- "{r.title}"{where}: {verdict} (decided {day}).{why}')
    if len(records) > SHOWN_AT_MOST:
        lines.append(f"- and {len(records) - SHOWN_AT_MOST} more: run `dowel decisions {rel}` to see them all.")
    lines.append("If your edit would go against one of these, stop and ask the developer before making it.")
    return "\n".join(lines)


def _state_dir() -> Path:
    path = Path.home() / ".dowel" / "hook"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _already_shown(session: str, key: str) -> bool:
    """True if this was shown earlier in the session; records it otherwise."""
    file = _state_dir() / f"{hashlib.sha256(session.encode()).hexdigest()[:16]}.json"
    try:
        seen = set(json.loads(file.read_text())) if file.exists() else set()
    except ValueError:
        seen = set()
    if key in seen:
        return True
    file.write_text(json.dumps(sorted(seen | {key})))
    return False


def before_edit(event: dict) -> dict | None:
    """Decide what to say when a file is opened or edited. None means: nothing to add, let it through."""
    tool_input = event.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    tool = event.get("tool_name")
    if tool not in _TOOLS or not target:
        return None
    cwd = Path(event.get("cwd") or os.getcwd())
    full = (Path(target) if Path(target).is_absolute() else cwd / target).resolve()
    repo = repo_of(full.parent if full.parent.exists() else cwd)
    if repo is None:
        return None
    try:
        rel = full.relative_to(repo.resolve()).as_posix()
    except ValueError:
        return None
    if tool == "Read":
        pass  # reading anything is fine; only writing to protected files is held
    elif rel.startswith(GUARDS + "/"):
        return _decision("deny", "This is a guard test written from a decision the developer made. It is not to be "
                                 "edited. If the code can no longer satisfy it, stop and tell the developer.")
    elif rel.startswith(files.FOLDER + "/"):
        return _decision("ask", "This file records a decision a developer made about the code. "
                                "Changing it is the developer's call.")
    records = for_file(repo, rel)
    if not records:
        return None
    text = full.read_text(errors="replace") if full.is_file() else ""
    symbol = edited_symbol(text, tool_input) if tool in _EDIT_TOOLS else None
    records = ranked(records, symbol)
    if tool in _EDIT_TOOLS and line.is_open():
        held = _ask_out_loud(rel, records, symbol, tool_input)
        if held is not None:
            return held
    key = rel + "|" + ",".join(sorted(r.uid for r in records))
    if _already_shown(str(event.get("session_id") or "no-session"), key):
        return None
    return _context(describe(rel, records))


def _ask_out_loud(rel: str, records: list[files.Record], symbol: str | None, tool_input: dict) -> dict | None:
    """Someone is on the call: ask them before changing code they made a decision to keep.

    Only for a decision about the very function being edited, or about the whole file. None means
    this edit does not need asking about, and the usual path continues.
    """
    about = next((r for r in records if r.stance == "keep"
                  and (not r.symbol or (symbol and r.symbol.rsplit(".", 1)[-1] == symbol))), None)
    if about is None:
        return None
    change = (tool_input.get("new_string") or tool_input.get("content") or "").strip().splitlines()
    got = line.ask({"file": rel, "symbol": about.symbol, "decision": about.title, "reason": about.reason,
                    "uid": about.uid, "change": "\n".join(change[:6])})
    if got is None:
        return _decision("ask", f'This edit changes code under a decision: "{about.title}" The developer did not '
                                "answer when asked out loud, so it needs their approval here.")
    said = f' They said: "{got.get("said", "")}"' if got.get("said") else ""
    if got.get("allow"):
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "permissionDecisionReason": "The developer allowed this out loud.",
            "additionalContext": f'Dowel asked the developer out loud about "{about.title}" and they allowed '
                                 f"this change.{said}"}}
    return _decision("deny", f'Dowel asked the developer out loud about "{about.title}" and they refused this '
                             f"change.{said} Do not make this edit; follow what they said.")


def _decision(verdict: str, reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": verdict,
                                   "permissionDecisionReason": reason}}


def _context(text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}


def run_key(repo: Path) -> str:
    """The folder name Dowel keeps this repository's local files under. Must match store.runs_dir."""
    return hashlib.sha256(str(repo).encode()).hexdigest()[:12]


def note_prompt(event: dict) -> None:
    """Keep what the developer asked for, on this machine only, for the next review to compare against."""
    prompt = (event.get("prompt") or "").strip()
    repo = repo_of(Path(event.get("cwd") or os.getcwd()))
    if not prompt or repo is None:
        return
    folder = Path.home() / ".dowel" / "runs" / run_key(repo.resolve())
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "prompts.jsonl").open("a") as out:
        out.write(json.dumps({"at": time.time(), "session": event.get("session_id") or "",
                              "prompt": prompt[:4000]}) + "\n")


MATCHER = "Read|Edit|Write|MultiEdit|NotebookEdit"


def install(repo: Path, command: str) -> Path:
    """Register this hook for one repository, in the settings file that is not committed.

    The command is a path on this machine, so it does not belong in a file the team shares.
    Anything already in the file is kept.
    """
    target = repo / ".claude" / "settings.local.json"
    target.parent.mkdir(exist_ok=True)
    settings = json.loads(target.read_text()) if target.exists() else {}
    hooks = settings.setdefault("hooks", {})
    # Usually this returns in about 50 ms. The limit is long because, with a call open, it waits
    # for the developer to answer out loud.
    entry = {"type": "command", "command": command, "timeout": 120}
    for event, matcher in (("PreToolUse", MATCHER), ("UserPromptSubmit", None)):
        groups = hooks.setdefault(event, [])
        if any(h.get("command") == command for g in groups for h in g.get("hooks", [])):
            continue  # already installed
        groups.append({"matcher": matcher, "hooks": [entry]} if matcher else {"hooks": [entry]})
    target.write_text(json.dumps(settings, indent=2) + "\n")
    return target


def main() -> None:
    try:
        event = json.load(sys.stdin)
        kind = event.get("hook_event_name")
        if kind == "PreToolUse":
            reply = before_edit(event)
            if reply is not None:
                print(json.dumps(reply))
        elif kind == "UserPromptSubmit":
            note_prompt(event)
    except Exception:  # noqa: BLE001, S110 - a fault in this hook must never stop the developer's agent
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
