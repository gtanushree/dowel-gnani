"""What a change touched, sorted by whether anyone asked for it.

Every changed file gets one of four labels:

- asked for: what the developer asked for names this file or something defined in it
- needed for it: a test of an asked-for file, or a file that uses one
- mechanical: lock files, generated code, pure renames, Dowel's own records
- not explained: everything else

The labels are worked out from names and text, with no model, so they are the same every time.
"What the developer asked for" is their own prompt to the agent when the hook has recorded it,
then the commit messages and the branch name. When none of those says anything usable, the map
says so instead of guessing: an agent's commit message is not evidence of what its user wanted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from dowel import decisions as files
from dowel import symbols, weakened
from dowel.gitops import changed_lines, run_git
from dowel.plan import FileDiff, skip_reason, split_diff

ASKED, NEEDED, MECHANICAL, UNEXPLAINED = "asked for", "needed for it", "mechanical", "not explained"
_GENERIC = {"main", "index", "utils", "util", "helpers", "common", "config", "init", "test", "tests", "types",
            "update", "fix", "change", "code", "file", "files", "make", "add", "the", "and", "for"}


@dataclass(frozen=True)
class Entry:
    path: str
    label: str
    why: str
    added: int
    removed: int


@dataclass(frozen=True)
class Scope:
    asked: str  # the text taken as what was asked for
    source: str  # where that text came from: "your prompt" | "commit messages" | ""
    entries: list[Entry]

    def of(self, label: str) -> list[Entry]:
        return [e for e in self.entries if e.label == label]


def _words(text: str) -> set[str]:
    """Words and identifier pieces, lower-cased: `load_config` gives load_config, load and config."""
    out: set[str] = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text):
        out.add(token.lower())
        for piece in re.split(r"_|(?<=[a-z0-9])(?=[A-Z])", token):
            if len(piece) >= 3:
                out.add(piece.lower())
    return out


def _named(name: str, wanted: set[str]) -> bool:
    """Whether what was asked mentions this name, allowing for plurals and endings (deliver, delivery)."""
    name = name.lower()
    if len(name) < 4 or name in _GENERIC:
        return False
    return any(len(w) >= 4 and (w.startswith(name) or name.startswith(w)) for w in wanted)


def what_was_asked(repo: Path, merge_base: str, prompts_file: Path | None) -> tuple[str, str]:
    """The developer's own words if recorded, else the commit messages. Returns (text, source)."""
    since = run_git(repo, "show", "-s", "--format=%ct", merge_base).strip()
    if prompts_file is not None and prompts_file.is_file():
        said = []
        for line in prompts_file.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not since or float(row.get("at", 0)) >= float(since):
                said.append(str(row.get("prompt", "")))
        if said:
            return "\n".join(said), "your prompt"
    # Commits that only record decisions say nothing about what the change was asked to do.
    log = run_git(repo, "log", "--format=%s%n%b", f"{merge_base}..HEAD", "--", ".", ":(exclude).dowel").strip()
    return (log, "commit messages") if len(_words(log) - _GENERIC) >= 2 else ("", "")


def build(repo: Path, merge_base: str, prompts_file: Path | None = None) -> Scope:
    asked, source = what_was_asked(repo, merge_base, prompts_file)
    wanted = _words(asked) - _GENERIC
    diff = run_git(repo, "diff", "-M", "--unified=0", "--no-color", merge_base)
    renamed = {row.split("\t")[2] for row in run_git(repo, "diff", "-M", "--name-status", merge_base).splitlines()
               if row.startswith("R100") and row.count("\t") == 2}
    ranges = changed_lines(diff)
    changed: list[FileDiff] = split_diff(diff)

    labels: dict[str, tuple[str, str]] = {}
    for f in changed:
        if f.path.startswith(".dowel/"):
            labels[f.path] = (MECHANICAL, "Dowel's own record")
        elif f.path in renamed:
            labels[f.path] = (MECHANICAL, "renamed, contents unchanged")
        elif (reason := skip_reason(f)) and reason != "only deletions":
            labels[f.path] = (MECHANICAL, reason)
    if not wanted:
        for f in changed:
            labels.setdefault(f.path, (UNEXPLAINED, "nothing recorded says what was asked for"))
        return Scope("", "", _entries(changed, labels))

    stems: dict[str, str] = {}  # stem of an asked-for file -> its path
    for f in changed:
        if f.path in labels:
            continue
        stem = PurePosixPath(f.path).stem.lower().removeprefix("test_").removesuffix("_test")
        source_text = (repo / f.path).read_text(errors="replace") if (repo / f.path).is_file() else ""
        named = [n for n in (symbols.touched(f.path, source_text, ranges.get(f.path, [])) or []) if _named(n, wanted)]
        if weakened.TEST_FILE.search(f.path):
            continue  # tests are placed by what they test, below
        if _named(stem, wanted):
            labels[f.path] = (ASKED, f"{source} names {PurePosixPath(f.path).name.rsplit('.', 1)[0]}")
        elif named:
            labels[f.path] = (ASKED, f"{source} names {named[0]}")
        elif any(_named(part, wanted) for part in PurePosixPath(f.path).parts[:-1]):
            labels[f.path] = (ASKED, f"{source} names the {PurePosixPath(f.path).parts[-2]} folder")
        if labels.get(f.path, ("",))[0] == ASKED:
            stems[stem] = f.path

    for f in changed:
        if f.path in labels:
            continue
        stem = PurePosixPath(f.path).stem.lower().removeprefix("test_").removesuffix("_test")
        text = (repo / f.path).read_text(errors="replace").lower() if (repo / f.path).is_file() else ""
        if weakened.TEST_FILE.search(f.path) and stem in stems:
            labels[f.path] = (NEEDED, f"tests {stems[stem]}")
        elif used := next((path for s, path in stems.items() if re.search(rf"\b{re.escape(s)}\b", text)), None):
            labels[f.path] = (NEEDED, f"uses {used}")
        else:
            labels[f.path] = (UNEXPLAINED, f"nothing in {source} points to it")
    return Scope(asked, source, _entries(changed, labels))


def _entries(changed: list[FileDiff], labels: dict[str, tuple[str, str]]) -> list[Entry]:
    order = {UNEXPLAINED: 0, ASKED: 1, NEEDED: 2, MECHANICAL: 3}
    out = [Entry(f.path, *labels[f.path], f.added, f.removed) for f in changed]
    return sorted(out, key=lambda e: (order[e.label], -(e.added + e.removed), e.path))


# Re-exported so callers that only need the folder name do not import the decisions module too.
DECISIONS = files.FOLDER
