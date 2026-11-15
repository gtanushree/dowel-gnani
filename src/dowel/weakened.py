"""Tests made weaker by a change, found from the diff alone.

Coding agents are known to make a failing test pass by changing the test: deleting it, removing
its assertions, marking it skipped, or renaming the file so it stops running. Each of those leaves
a mark in the diff that can be found without a model, which means the result is the same every
time and costs nothing.

What this does not catch: an expected value quietly loosened (`== 3` to `>= 0`), or a test rewritten
to assert something weaker in the same number of lines. Those need judgement and are left to the review.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

TEST_FILE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/"
    r"|(^|/)test_[^/]+\.py$|_test\.(py|go|rb|rs)$|\.(test|spec)\.[cm]?[jt]sx?$"
    r"|Tests?\.(java|kt|cs|swift)$|_spec\.rb$"
)
_ASSERT = re.compile(
    r"^\s*assert\b|\bself\.assert\w+\(|\bpytest\.raises\(|\bexpect\(|\bassert\.\w+\(|\brequire\.\w+\("
    r"|\bt\.(Error|Errorf|Fatal|Fatalf|Fail|FailNow)\(|\bassert[A-Z]\w*\(|\bassert(_eq|_ne)?!\(|\.should\b"
)
_SKIP = re.compile(
    r"@pytest\.mark\.(skip|skipif|xfail)|\bpytest\.skip\(|@unittest\.skip|\b(it|test|describe)\.(skip|todo)\("
    r"|\bx(it|describe|test)\(|\bt\.Skip(f|Now)?\(|#\[ignore\]|@Disabled\b|@Ignore\b"
)
_TRIVIAL = re.compile(
    r"^\s*assert\s+True\s*$|\bexpect\(\s*true\s*\)\.toBe\(\s*true\s*\)|^\s*assert\s+1\s*==\s*1\s*$"
    r"|\bexpect\(\s*1\s*\)\.toBe\(\s*1\s*\)|\bassertTrue\(\s*true\s*\)"
)
_TEST_NAME = re.compile(
    r"\bdef\s+(test_\w+)|\b(?:it|test)(?:\.\w+)?\(\s*['\"`]([^'\"`]+)|\bfunc\s+(Test\w+)|\bfn\s+(test_\w+)|\bvoid\s+(test\w+)\s*\("
)


@dataclass(frozen=True)
class Weakening:
    file: str
    kind: str  # deleted | disabled | tests removed | skipped | assertions removed | trivial assertion | guard edited
    detail: str
    lines: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """Identifies this weakening, so a recorded reason covers this one and not the next."""
        return hashlib.sha256(f"{self.file}|{self.kind}|{'|'.join(sorted(self.lines))}".encode()).hexdigest()[:16]


def _names(lines: list[str]) -> set[str]:
    return {next(g for g in m.groups() if g) for line in lines for m in _TEST_NAME.finditer(line)}


def _per_file(diff: str) -> dict[str, tuple[list[str], list[str]]]:
    """Path -> (lines the change removed, lines it added)."""
    out: dict[str, tuple[list[str], list[str]]] = {}
    old = new = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            old = None if line[4:].strip() == "/dev/null" else line[4:].strip().removeprefix("a/")
        elif line.startswith("+++ "):
            new = None if line[4:].strip() == "/dev/null" else line[4:].strip().removeprefix("b/")
            out.setdefault(new or old or "", ([], []))
        elif line.startswith("-") and not line.startswith("---") and (new or old):
            out[new or old or ""][0].append(line[1:])
        elif line.startswith("+") and not line.startswith("+++") and (new or old):
            out[new or old or ""][1].append(line[1:])
    return out


def find(diff: str, name_status: str, guards: str = ".dowel/guards/") -> list[Weakening]:
    """Every way this change made the tests weaker.

    `diff` is a unified diff of the change; `name_status` is `git diff -M --name-status` for it.
    """
    found: list[Weakening] = []
    gone: set[str] = set()
    for row in name_status.splitlines():
        parts = row.split("\t")
        status = parts[0][:1] if parts else ""
        if status == "D" and len(parts) >= 2 and TEST_FILE.search(parts[1]):
            found.append(Weakening(parts[1], "deleted", "the test file was deleted"))
            gone.add(parts[1])
        elif status == "R" and len(parts) >= 3 and TEST_FILE.search(parts[1]) and (
                not TEST_FILE.search(parts[2]) or PurePosixPath(parts[1]).suffix != PurePosixPath(parts[2]).suffix):
            found.append(Weakening(parts[1], "disabled", f"renamed to {parts[2]}, which the test runner will not pick up"))
            gone |= {parts[1], parts[2]}
        if status in ("M", "D", "R") and len(parts) >= 2 and any(p.startswith(guards) for p in parts[1:]):
            path = next(p for p in parts[1:] if p.startswith(guards))
            found.append(Weakening(path, "guard edited", "a guard test written from a decision was changed"))
            gone.add(path)

    for path, (removed, added) in _per_file(diff).items():
        if path in gone or not TEST_FILE.search(path):
            continue
        gained = _names(added) - _names(removed)
        lost = sorted(_names(removed) - _names(added))
        if len(gained) >= len(lost):
            lost = []  # as many new names as old ones went: tests were renamed, not removed
        if lost:
            found.append(Weakening(path, "tests removed", f"{len(lost)} test{'s' if len(lost) != 1 else ''} removed: "
                                   + ", ".join(lost[:5]), tuple(lost)))
        skips = [line.strip() for line in added if _SKIP.search(line)]
        skips = [line for line in skips if line not in {r.strip() for r in removed}]
        if skips and not gained:  # a skip marker that arrives with a brand-new test takes nothing away
            found.append(Weakening(path, "skipped", f"{len(skips)} test{'s' if len(skips) != 1 else ''} marked to be skipped",
                                   tuple(skips[:8])))
        trivial = [line.strip() for line in added if _TRIVIAL.search(line)]
        if trivial:
            found.append(Weakening(path, "trivial assertion", "an assertion that can never fail was added",
                                   tuple(trivial[:8])))
        kept = {line.strip() for line in added}
        dropped = [line.strip() for line in removed if _ASSERT.search(line) and line.strip() not in kept]
        fewer = len(dropped) - sum(1 for line in added if _ASSERT.search(line) and not _TRIVIAL.search(line)
                                   and line.strip() not in {r.strip() for r in removed})
        if fewer > 0 and not lost:  # a removed test already accounts for its own assertions
            found.append(Weakening(path, "assertions removed",
                                   f"{fewer} fewer assertion{'s' if fewer != 1 else ''} than before", tuple(dropped[:8])))
    return sorted(found, key=lambda w: (w.file, w.kind))
