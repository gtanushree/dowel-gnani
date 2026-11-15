"""Decisions a repository already made, read from its history, with no questions asked.

The hardest thing about a record of decisions is the first one. A repository's history is full
of them: every commit that fixed something is somebody deciding "it must work like this". This
proposes each fix that is still in the code as a decision to keep, tied to the function the fix
landed in, with the commit as the reason. The developer takes the ones worth holding on to.

No model is used: candidates come from commit subjects and from which added lines survive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dowel import symbols
from dowel.gitops import run_git
from dowel.memory import Memory

_FIX = re.compile(r"\b(fix(e[sd])?|bug|hotfix|regression|restore[sd]?|guard|prevent[s]?|must|never|always)\b", re.I)
_NOISE = re.compile(r"^\s*([#/*\-]|\"\"\"|'''|import |from |\}|\{|\)|\]|else:?|pass|return;?)?\s*$")
_SKIP = re.compile(r"\b(typo|lint|format|bump|merge|wip|readme|docs?|comment|rename|version)\b", re.I)


@dataclass(frozen=True)
class Candidate:
    commit: str
    date: str
    subject: str
    file: str
    line: int
    symbol: str

    @property
    def title(self) -> str:
        subject = re.sub(r"^(fix|bug|hotfix)(\([^)]*\))?[:!]?\s*", "", self.subject, flags=re.I).strip()
        return (subject[:1].upper() + subject[1:]).rstrip(".") + "."

    @property
    def reason(self) -> str:
        return f"Fixed on {self.date} in {self.commit}; taken from the history, not yet confirmed by a person."


def find(repo: Path, limit: int = 300, at_most: int = 25) -> list[Candidate]:
    """Fixes from the last `limit` commits whose lines are still in the code, newest first."""
    memory = Memory(repo)
    try:
        decided = {(where.file, where.symbol) for _, where in memory.located()}
    finally:
        memory.close()
    out: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    log = run_git(repo, "log", f"-n{limit}", "--no-merges", "--format=%h%x09%ad%x09%s", "--date=short")
    for row in log.splitlines():
        commit, _, rest = row.partition("\t")
        date, _, subject = rest.partition("\t")
        if not _FIX.search(subject) or _SKIP.search(subject):
            continue
        current = ""
        added: dict[str, list[str]] = {}
        for line in run_git(repo, "show", "--format=", "--unified=0", "--no-color", "-M", commit).splitlines():
            if line.startswith("+++ "):
                current = "" if line[4:].strip() == "/dev/null" else line[4:].strip().removeprefix("b/")
            elif line.startswith("+") and not line.startswith("+++") and current:
                text = line[1:].strip()
                if len(text) >= 12 and not _NOISE.match(text):
                    added.setdefault(current, []).append(text)
        for file, lines in added.items():
            path = repo / file
            if symbols.language_of(file) is None or not path.is_file() or file.startswith(".dowel/"):
                continue
            source = path.read_text(errors="replace")
            rows = [text.strip() for text in source.splitlines()]
            alive = [text for text in lines if text in rows]
            if not alive or len(alive) / len(lines) < 0.6:
                continue  # the fix has gone or been rewritten: that is `dowel history`'s business
            where = symbols.enclosing(file, source, rows.index(alive[0]) + 1)
            if where is None or (file, where.name) in decided or (file, where.name) in seen:
                continue
            seen.add((file, where.name))
            out.append(Candidate(commit, date, subject, file, rows.index(alive[0]) + 1, where.name))
            if len(out) >= at_most:
                return out
    return out
