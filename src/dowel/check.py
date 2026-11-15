"""`dowel check`: the things about a change that should stop it until a person has looked.

- A test was made weaker and nobody has said why.
- A guard test written from a decision was edited.
- Code under a recorded decision changed, and it has not been checked against that decision
  (or it was checked and goes against it).
- Two decisions in force disagree about the same code.

Everything except judging a changed function against its decision is worked out from git and the
decision files, with no model. That is deliberate: a check that fails a build has to give the same
answer every time.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from dowel import decisions as files
from dowel import weakened
from dowel.gitops import run_git
from dowel.memory import Decision, Located, Memory

GUARDS = ".dowel/guards"
_CLAIM = re.compile(r"\b(all\s+)?tests?\s+(now\s+|still\s+)?pass(es|ed|ing)?\b|\bunrelated\s+(test\s+)?failures?\b"
                    r"|\bpre-?existing\s+(test\s+)?failures?\b|\bfailing\s+tests?\s+(are|is|were)\s+unrelated\b", re.IGNORECASE)


def run_guards(repo: Path) -> tuple[bool, str] | None:
    """Run the guard tests written from decisions. None when there are none."""
    folder = repo / GUARDS
    if not folder.is_dir() or not any(folder.glob("test_*.py")):
        return None
    try:
        done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", GUARDS], cwd=repo,
                              capture_output=True, text=True, timeout=300, check=False,
                              env={"PYTHONPATH": str(repo), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return False, "the guard tests did not finish in five minutes"
    lines = (done.stdout + done.stderr).strip().splitlines()
    said = [line for line in lines if "AssertionError" in line or line.startswith(("FAILED", "ERROR"))][:4]
    return done.returncode == 0, "\n".join(said or lines[-3:])


def claims(repo: Path, merge_base: str) -> list[str]:
    """Lines in this change's commit messages that say the tests are fine."""
    log = run_git(repo, "log", "--format=%s%n%b", f"{merge_base}..HEAD", check=True)
    return list(dict.fromkeys(line.strip() for line in log.splitlines() if _CLAIM.search(line)))[:3]


ACCEPTED = "test_weakened"  # the kind of record that says "this weakening was looked at, and here is why"


@dataclass(frozen=True)
class Problem:
    kind: str  # test | decision | conflict
    where: str
    what: str
    lines: tuple[str, ...] = ()
    weakening: weakened.Weakening | None = None
    decision: Decision | None = None
    located: Located | None = None


@dataclass
class Report:
    problems: list[Problem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


def _accepted(repo: Path) -> set[tuple[str, str]]:
    records = files.read_all(repo)
    gone = {old for r in records for old in r.supersedes} | {r.retires for r in records if r.kind == "retire"}
    return {(r.path, r.body_hash) for r in records
            if r.kind == "decision" and r.bug_class == ACCEPTED and r.uid not in gone}


def gather(repo: Path, merge_base: str) -> Report:
    """Look at everything changed since `merge_base`, including edits not yet committed."""
    status = run_git(repo, "diff", "-M", "--name-status", merge_base, check=True)
    diff = run_git(repo, "diff", "-M", "--unified=0", "--no-color", merge_base, check=True)
    report = Report(changed=[row.split("\t")[-1] for row in status.splitlines() if row])

    done = _accepted(repo)
    for w in weakened.find(diff, status):
        if (w.file, w.key) not in done:
            report.problems.append(Problem("test", w.file, w.detail, w.lines, weakening=w))

    guards = run_guards(repo)
    if guards is not None and not guards[0]:
        report.problems.append(Problem("guard", GUARDS, "a guard test written from a decision fails on this change",
                                       tuple(guards[1].splitlines())))
    said = claims(repo, merge_base)
    contradicted = [p for p in report.problems if p.kind in ("test", "guard")]
    if said and contradicted:
        report.problems.append(Problem(
            "claim", "commit messages", f'the change says "{said[0]}", but {len(contradicted)} thing'
            f"{'s' if len(contradicted) != 1 else ''} above say otherwise", tuple(said)))
    elif said:
        report.notes.append(f'The change says "{said[0]}". Dowel did not run the project\'s tests, so this is not verified.')

    memory = Memory(repo)
    try:
        for d, where in memory.touched(report.changed):
            if d.stance in ("keep", "change") and d.bug_class != ACCEPTED:
                report.problems.append(Problem(
                    "decision", f"{where.file} · {where.symbol}",
                    f"this code changed since a decision about it, and has not been checked against it: {d.rule}",
                    decision=d, located=where))
        for a, b in memory.conflicts():
            report.problems.append(Problem("conflict", f"{a.file} · {a.symbol}",
                                           f"two decisions in force disagree: decision:{a.id} and decision:{b.id}"))
        lost = len(memory.orphaned())
        if lost:
            report.notes.append(f"{lost} decision{'s' if lost != 1 else ''} could not be matched to any code "
                                "(see `dowel memory`). Not counted against this change.")
    finally:
        memory.close()
    return report


def accept(repo: Path, problem: Problem, because: str) -> str:
    """Record why a weakened test is acceptable. Returns the file written."""
    w = problem.weakening
    if w is None:
        raise ValueError("only a weakened test can be accepted this way; retire or re-make a decision instead")
    if not because.strip():
        raise ValueError("a reason is required: it is what the next person, and the next agent, will read")
    return files.write(repo, files.Record(
        uid=files.new_uid(), kind="decision", created=time.time(), stance="keep", bug_class=ACCEPTED,
        title=f"Tests in {w.file}: {w.detail}.", reason=because.strip(), path=w.file, body_hash=w.key,
        at=files.head(repo), given="typed"))


async def judge(repo: Path, merge_base: str, report: Report) -> None:
    """Ask a model whether each changed function still honours its decision. Costs a little."""
    from dowel import prompts
    from dowel.config import Settings
    from dowel.llm import Claude, Ledger, LLMError
    from dowel.memory import Contradiction

    settings, claude = Settings(), Claude(Ledger(0.50))
    memory = Memory(repo)
    kept: list[Problem] = []
    try:
        for p in report.problems:
            if p.kind != "decision" or p.decision is None or p.located is None:
                kept.append(p)
                continue
            diff = run_git(repo, "diff", "--unified=6", "--no-color", merge_base, "--", p.located.file, check=True)
            try:
                ruling = await claude.structured(
                    model=settings.models.assist, system=prompts.CONTRADICTS, output=Contradiction, effort="low",
                    user=(f"Earlier decision about {p.located.file} ({p.located.symbol}): {p.decision.rule}\n\n"
                          f"<diff>\n{diff[:20_000]}\n</diff>"))
            except LLMError as exc:
                kept.append(p)
                report.notes.append(f"could not check {p.where}: {exc}")
                continue
            if ruling.contradicts:
                kept.append(Problem("decision", p.where, f"goes against a decision of {p.decision.day}: "
                                    f"{ruling.explanation} The decision was: {p.decision.rule}",
                                    decision=p.decision, located=p.located))
            else:
                memory.mark_checked(p.decision.id, p.located.fingerprint)
    finally:
        memory.close()
    report.problems = kept
