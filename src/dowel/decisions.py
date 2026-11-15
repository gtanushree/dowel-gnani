"""Decisions as small files in the repository, so they travel with the code and are reviewed like it.

One file per decision under `.dowel/decisions/`. A file is a draft until it is committed, and may
be rewritten; once it is in git it is never edited. Changing your mind writes a new file that names
the one it replaces, and retiring a decision writes a small file that names the one it retires. So
two people rarely touch the same file, and the history of a decision is the files themselves.

Only the decision and the reason are written, in the developer's words. Transcripts and audio are
never written here. Dowel does not commit these files: the developer sees them in `git status`,
can edit or delete them, and commits them like any other change.

Text in these files may have been written by someone else and will be shown to coding agents, so
anything that reads like an instruction to an AI is held back from agents and flagged.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

FOLDER = ".dowel/decisions"

# Deliberately narrower than the screen used on code: a real decision may well say "the agent
# must not remove this". What is held back is text that tries to take over whoever reads it.
_TAKEOVER = re.compile(
    r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|above|prior|earlier|all|your)\b.{0,30}\b(instruction|prompt|rule)s?"
    r"|\byou are (now )?(an? )?(ai|assistant|language model|agent)\b"
    r"|\bsystem prompt\b|\bnew instructions?:"
    r"|\b(approve|lgtm|skip (the )?review)\b.{0,30}\b(this|pr|change|everything)\b"
    r"|\b(run|execute)\b.{0,20}\b(curl|wget|bash|sh|rm -rf)\b",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class Record:
    uid: str
    kind: str  # decision | retire
    created: float
    title: str = ""  # what was decided about
    reason: str = ""  # the developer's words
    stance: str = ""  # keep | change | unsure
    bug_class: str = ""
    path: str = ""
    symbol: str = ""
    body_hash: str = ""
    at: str = ""  # the commit the decision was made against
    given: str = ""  # spoken | typed | ticked
    supersedes: tuple[str, ...] = ()
    retires: str = ""
    file: str = ""  # where this record is stored, relative to the repository

    @property
    def suspect(self) -> bool:
        return bool(_TAKEOVER.search(f"{self.title}\n{self.reason}"))


def new_uid() -> str:
    """Sorts by time, and two people making decisions at once do not collide."""
    return f"{int(time.time() * 1000):011x}{secrets.token_hex(3)}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "decision"


_FIELDS = ("uid", "kind", "stance", "bug_class", "path", "symbol", "body_hash", "at", "given", "created",
           "supersedes", "retires")


def render(record: Record) -> str:
    head = []
    for name in _FIELDS:
        value = getattr(record, name)
        if value in ("", ()):
            continue
        head.append(f"{name}: {json.dumps(list(value) if isinstance(value, tuple) else value)}")
    head.append(f'on: "{time.strftime("%Y-%m-%d", time.localtime(record.created))}"')  # for people; `created` is what is read
    body = record.title.strip()
    if record.reason.strip():
        body += "\n\n" + record.reason.strip()
    return "---\n" + "\n".join(head) + "\n---\n" + body + "\n"


def parse(text: str, file: str = "") -> Record | None:
    """None for anything that is not a well-formed record: a stray file must not break a review."""
    match = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not match:
        return None
    values: dict[str, object] = {}
    for line in match.group(1).splitlines():
        name, sep, raw = line.partition(":")
        if not sep or name.strip() not in _FIELDS:
            continue
        try:
            values[name.strip()] = json.loads(raw.strip())
        except ValueError:
            return None
    uid, kind, created = values.get("uid"), values.get("kind"), values.get("created")
    if not isinstance(uid, str) or kind not in ("decision", "retire") or not isinstance(created, int | float):
        return None
    title, _, reason = match.group(2).strip().partition("\n\n")
    supersedes = values.get("supersedes") or []

    def text_of(key: str) -> str:
        return str(values.get(key) or "")

    return Record(
        uid=uid, kind=str(kind), created=float(created), title=title.strip(), reason=reason.strip(),
        stance=text_of("stance"), bug_class=text_of("bug_class"), path=text_of("path"), symbol=text_of("symbol"),
        body_hash=text_of("body_hash"), at=text_of("at"), given=text_of("given"),
        supersedes=tuple(str(s) for s in supersedes) if isinstance(supersedes, list) else (),
        retires=text_of("retires"), file=file,
    )


def read_all(repo: Path) -> list[Record]:
    folder = repo / FOLDER
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.md")):
        record = parse(path.read_text(encoding="utf-8-sig", errors="replace"), f"{FOLDER}/{path.name}")
        if record is not None:
            out.append(record)
    return out


def write(repo: Path, record: Record) -> str:
    """Write a record and return its path relative to the repository."""
    folder = repo / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    name = record.file or f"{FOLDER}/{record.uid}-{_slug(record.title or record.kind)}.md"
    (repo / name).write_text(render(record), encoding="utf-8")
    return name


def committed(repo: Path, file: str) -> bool:
    """Whether a record is already part of the repository's history, and so must not be edited."""
    try:
        done = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"HEAD:{file}"],
                              capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def head(repo: Path) -> str:
    try:
        done = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""
