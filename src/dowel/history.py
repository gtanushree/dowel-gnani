"""Fixes in a repository's history that a later change quietly undid.

A fix goes in. Weeks later a refactor, often by an agent, removes the lines the fix added, and
nothing notices because nothing was written down. This reads the history for that shape:

1. a commit whose message says it fixes or restores something,
2. whose added lines are, for the most part, no longer in the file,
3. while the file itself still exists.

It then names the later commit that took the lines out. No model is used. The result is a list
of places worth a look, not a verdict: a fix can be removed because it was replaced by a better one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dowel.gitops import run_git

_FIX = re.compile(r"\b(fix(e[sd])?|bug|hotfix|regression|restore[sd]?|revert|patch(e[sd])?|guard|prevent[s]?)\b", re.IGNORECASE)
_NOISE = re.compile(r"^\s*([#/*\-]|\"\"\"|'''|import |from |\}|\{|\)|\]|else:?|pass|return;?)?\s*$")


@dataclass(frozen=True)
class Undone:
    fix: str  # short hash of the fixing commit
    fix_subject: str
    fix_date: str
    file: str
    gone: tuple[str, ...]  # lines the fix added that are no longer there
    of: int  # how many meaningful lines the fix added to this file
    removed_by: str  # short hash and subject of the commit that took the first of them out, if found


def _added(repo: Path, commit: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current = ""
    for line in run_git(repo, "show", "--format=", "--unified=0", "--no-color", "-M", commit).splitlines():
        if line.startswith("+++ "):
            current = "" if line[4:].strip() == "/dev/null" else line[4:].strip().removeprefix("b/")
        elif line.startswith("+") and not line.startswith("+++") and current:
            text = line[1:].strip()
            if len(text) >= 12 and not _NOISE.match(text):
                out.setdefault(current, []).append(text)
    return out


def find(repo: Path, limit: int = 300, share_gone: float = 0.6) -> list[Undone]:
    """Look at the last `limit` commits for fixes whose lines have since disappeared."""
    found: list[Undone] = []
    log = run_git(repo, "log", f"-n{limit}", "--no-merges", "--format=%h%x09%ad%x09%s", "--date=short")
    for row in log.splitlines():
        commit, _, rest = row.partition("\t")
        date, _, subject = rest.partition("\t")
        if not _FIX.search(subject):
            continue
        for file, lines in _added(repo, commit).items():
            path = repo / file
            if len(lines) < 2 or not path.is_file() or file.startswith(".dowel/"):
                continue
            now = {text.strip() for text in path.read_text(errors="replace").splitlines()}
            gone = [text for text in lines if text not in now]
            if len(gone) / len(lines) < share_gone:
                continue
            by = run_git(repo, "log", "-n1", "--format=%h %s", "-S", gone[0], f"{commit}..HEAD", "--", file).strip()
            found.append(Undone(commit, subject, date, file, tuple(gone[:4]), len(lines), by))
    return found
