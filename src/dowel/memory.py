"""Dowel's memory: what the author decided, what they asked not to hear again, and what was asked.

Design, from research into how reviewers and agent memories behave in practice:

- A few hundred short records per repository do not need a graph or a vector store. Plain rows,
  looked up by the code they are about, are enough. What matters is knowing when a record has
  gone stale, so every decision is anchored to a function and checked against the code each time.
- A decision is tied to code, not to time. Whether it is fresh (the code is as it was), drifted
  (the code has changed since) or orphaned (the code cannot be found) is worked out each time
  it is read, from the code as it is now. Reading never changes a record.
- Code moves. A decision follows its function through a rename of the file, a rename of the
  function, or a move to another file in the change, by the fingerprint of the function's code.
- Noise rules never hide security findings or top-priority issues.
- Nothing is deleted: a replaced record points at what replaced it, and everything can be listed
  and forgotten by the author.

Decisions are files in the repository (see decisions.py). The database under ~/.dowel is an
index over those files, rebuilt whenever they change, plus things that stay on this machine:
noise rules, what was shown, and which versions of a function have already been checked.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import Field

from dowel import config, gitops, store, symbols
from dowel import decisions as files
from dowel.schemas import Evidence, Finding, Hypothesis, RunResult, Strict, Verdict
from dowel.store import ItemState

NEVER_SUPPRESS = {"access_or_security"}
SAME_BAR = 0.7  # the fast model's confidence before two findings are treated as the same
SAME_ELSEWHERE_BAR = 0.8  # a higher bar when the two findings are not about the same function

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, bug_class TEXT NOT NULL,
    stance TEXT NOT NULL, title TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL, symbol TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active', commit_sha TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, confirmed_at REAL NOT NULL, superseded_by INTEGER
);
CREATE TABLE IF NOT EXISTS noise (
    id INTEGER PRIMARY KEY, bug_class TEXT NOT NULL, scope TEXT NOT NULL, example TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', hits INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'shown', commit_sha TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS verified (
    decision_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, at REAL NOT NULL,
    PRIMARY KEY (decision_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS decisions_file ON decisions(file, status);
CREATE INDEX IF NOT EXISTS ledger_fp ON ledger(fingerprint);
"""

Same = Callable[[str, str], Awaitable[float | None]]
"""Asks the fast model whether two findings describe the same thing; returns a probability."""


class Contradiction(Strict):
    contradicts: bool = Field(description="True only if the new code clearly does the opposite of the decision.")
    line: int = Field(description="The line in the new code that shows it, or 0.")
    explanation: str = Field(description="One plain sentence: what the code now does against the decision.")


Judge = Callable[["Decision", str], Awaitable[Contradiction | None]]


@dataclass(frozen=True)
class Decision:
    id: int
    fingerprint: str
    bug_class: str
    stance: str  # keep | change | unsure
    title: str
    reason: str
    file: str
    symbol: str
    content_hash: str
    status: str  # active | superseded | forgotten
    created_at: float
    hash_v: int = 2  # 1: hash of the function's text; 2: fingerprint of its code tokens
    uid: str = ""  # the id of its file in the repository; empty for one kept only on this machine
    given: str = ""  # spoken | typed | ticked
    suspect: int = 0  # its text reads like an instruction to an AI, so agents are not shown it

    @property
    def day(self) -> str:
        return time.strftime("%d %b %Y", time.localtime(self.created_at))

    @property
    def rule(self) -> str:
        if self.stance == "keep":
            text = f"Intended: {self.title}"
        elif self.stance == "change":
            text = f"Not wanted: {self.title}"
        else:
            text = f"Undecided: {self.title}"
        return f"{text} ({self.reason})" if self.reason else text


def _text_hash(text: str) -> str:
    """How decisions made before token fingerprints were anchored. Kept so those still compare."""
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()[:16]


def _read(repo: Path, file: str) -> str | None:
    path = repo / file
    return path.read_text(errors="replace") if path.is_file() else None


def _anchor(repo: Path, file: str, line: int) -> tuple[str, str]:
    """The function a line sits in and a fingerprint of its code, so a later change can be noticed."""
    source = _read(repo, file)
    found = symbols.enclosing(file, source, line) if source is not None else None
    return (found.name, found.fingerprint) if found else ("", "")


@dataclass(frozen=True)
class Located:
    """Where a decision's code is now, and whether it still reads the way it did."""

    state: str  # fresh | drifted | orphaned
    file: str = ""
    symbol: str = ""
    first: int = 0
    fingerprint: str = ""
    how: str = ""  # "" (where it was) | renamed | moved


def _identity(bug_class: str, file: str, where: str) -> str:
    return hashlib.sha256(f"{bug_class}|{file}|{where}".encode()).hexdigest()[:16]


def fingerprint(finding: Finding, symbol: str) -> str:
    """What makes two findings "about the same thing": the kind of problem and the function."""
    return _identity(finding.hypothesis.bug_class, finding.file, symbol or "file")


class Memory:
    def __init__(self, repo: str | Path, db_path: Path | None = None) -> None:
        self.repo = Path(repo)
        self._db = sqlite3.connect(db_path or store.runs_dir(str(repo)) / "memory.db")
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        have = {row["name"] for row in self._db.execute("PRAGMA table_info(decisions)")}
        if "hash_v" not in have:
            # Decisions recorded before token fingerprints keep comparing the way they were made.
            self._db.execute("ALTER TABLE decisions ADD COLUMN hash_v INTEGER NOT NULL DEFAULT 1")
        for column, kind in (("uid", "TEXT NOT NULL DEFAULT ''"), ("given", "TEXT NOT NULL DEFAULT ''"),
                             ("suspect", "INTEGER NOT NULL DEFAULT 0")):
            if column not in have:
                self._db.execute(f"ALTER TABLE decisions ADD COLUMN {column} {kind}")
        self._renamed: dict[str, str] | None = None
        self.sync()

    # --- the files are the record; the database follows them ------------------

    def sync(self) -> None:
        """Bring the index in line with the decision files as they are in this checkout.

        A decision whose file is not here (another branch, a fresh clone before pulling) is not
        active here. One that another file supersedes or retires is not active either.
        """
        records = files.read_all(self.repo)
        present = {r.uid: r for r in records if r.kind == "decision"}
        known = {row["uid"] for row in self._db.execute("SELECT uid FROM decisions WHERE uid != ''")}
        for r in present.values():
            row = (r.stance, r.title, r.reason, r.path, r.symbol, r.body_hash, r.at, r.given, int(r.suspect), r.uid)
            if r.uid in known:  # a draft may have been edited by hand before it was committed
                self._db.execute(
                    "UPDATE decisions SET stance = ?, title = ?, reason = ?, file = ?, symbol = ?, content_hash = ?,"
                    " commit_sha = ?, given = ?, suspect = ? WHERE uid = ?", row)
            else:
                self._db.execute(
                    "INSERT INTO decisions (stance, title, reason, file, symbol, content_hash, commit_sha, given,"
                    " suspect, uid, fingerprint, bug_class, created_at, confirmed_at, hash_v)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2)",
                    (*row, _identity(r.bug_class, r.path, r.symbol or "file"), r.bug_class, r.created, r.created))
        replaced = {old for r in present.values() for old in r.supersedes}
        retired = {r.retires for r in records if r.kind == "retire"}
        for row in list(self._db.execute("SELECT id, uid FROM decisions WHERE uid != ''")):
            uid = row["uid"]
            status = ("absent" if uid not in present else "forgotten" if uid in retired
                      else "superseded" if uid in replaced else "active")
            self._db.execute("UPDATE decisions SET status = ? WHERE id = ?", (status, row["id"]))
        self._db.commit()

    def _write_decision(self, finding: Finding, item: ItemState, symbol: str, content_hash: str, given: str,
                        replaces: sqlite3.Row | None) -> None:
        """Write the developer's answer as a file. A draft not yet committed is rewritten in place."""
        draft = replaces is not None and replaces["uid"] and not files.committed(self.repo, self._file_of(replaces["uid"]))
        # A draft that already replaced a committed decision must keep saying so when it is rewritten,
        # or the committed one would come back into force beside it.
        earlier = next((r.supersedes for r in files.read_all(self.repo) if draft and r.uid == replaces["uid"]), ())
        record = files.Record(
            uid=replaces["uid"] if draft else files.new_uid(), kind="decision", created=time.time(),
            title=finding.title, reason=item.note.strip(), stance=item.answer or "",
            bug_class=finding.hypothesis.bug_class, path=finding.file, symbol=symbol, body_hash=content_hash,
            at=files.head(self.repo), given=given,
            supersedes=earlier if draft else () if replaces is None or not replaces["uid"] else (replaces["uid"],),
            file=self._file_of(replaces["uid"]) if draft else "",
        )
        files.write(self.repo, record)

    def _file_of(self, uid: str) -> str:
        return next((r.file for r in files.read_all(self.repo) if r.uid == uid), "")

    def _retire(self, uid: str, reason: str) -> None:
        """Take a decision out of force: delete it if it was never committed, else record that it is retired."""
        file = self._file_of(uid)
        if file and not files.committed(self.repo, file):
            # Deleting a draft must not bring back the committed decisions it had replaced.
            for older in next((r.supersedes for r in files.read_all(self.repo) if r.uid == uid), ()):
                self._retire(older, reason)
            (self.repo / file).unlink(missing_ok=True)
        elif file and uid not in {r.retires for r in files.read_all(self.repo) if r.kind == "retire"}:
            files.write(self.repo, files.Record(uid=files.new_uid(), kind="retire", created=time.time(),
                                                title=f"Retires {uid}", reason=reason, retires=uid))

    def export_local(self) -> int:
        """Write files for decisions that so far exist only on this machine. Returns how many."""
        done = 0
        for d in self.decisions():
            if d.uid:
                continue
            uid = files.new_uid()
            # A decision anchored the old way is re-anchored to the code as it is now.
            where = self.locate(d)
            body_hash = where.fingerprint if where.state != "orphaned" and where.fingerprint else d.content_hash
            files.write(self.repo, files.Record(
                uid=uid, kind="decision", created=d.created_at, title=d.title, reason=d.reason, stance=d.stance,
                bug_class=d.bug_class, path=where.file or d.file, symbol=where.symbol or d.symbol,
                body_hash=body_hash, at=files.head(self.repo), given=d.given))
            self._db.execute("UPDATE decisions SET uid = ?, content_hash = ?, hash_v = 2, file = ?, symbol = ?"
                             " WHERE id = ?", (uid, body_hash, where.file or d.file, where.symbol or d.symbol, d.id))
            done += 1
        self._db.commit()
        self.sync()
        return done

    def conflicts(self) -> list[tuple[Decision, Decision]]:
        """Pairs of decisions in force about the same code that say opposite things."""
        by_code: dict[str, list[Decision]] = {}
        for d in self.decisions():
            if d.stance in ("keep", "change"):
                by_code.setdefault(d.fingerprint, []).append(d)
        return [(a, b) for group in by_code.values() for i, a in enumerate(group) for b in group[i + 1:]
                if a.stance != b.stance]

    def close(self) -> None:
        self._db.close()

    # --- writing -----------------------------------------------------------

    def remember_answer(self, finding: Finding, item: ItemState, commit: str = "", given: str = "") -> None:
        """Record the author's latest answer to one finding. Safe to call on every edit of the answer."""
        symbol, content_hash = _anchor(self.repo, finding.file, finding.line)
        fp = fingerprint(finding, symbol)
        now = time.time()
        bug_class = finding.hypothesis.bug_class

        rule = self._db.execute(
            "SELECT id FROM noise WHERE bug_class = ? AND scope = ? AND example = ? AND status = 'active'",
            (bug_class, finding.file, finding.title),
        ).fetchone()
        if item.noise and rule is None:
            self._db.execute("INSERT INTO noise (bug_class, scope, example, created_at) VALUES (?, ?, ?, ?)",
                             (bug_class, finding.file, finding.title, now))
        elif not item.noise and rule is not None:
            self._db.execute("UPDATE noise SET status = 'forgotten' WHERE id = ?", (rule["id"],))

        current = self._db.execute(
            "SELECT id, uid, stance, reason FROM decisions WHERE fingerprint = ? AND status = 'active'", (fp,)
        ).fetchone()
        if item.answer is None:
            if current is not None:  # the author cleared their answer
                self._forget_row(current, "answer cleared")
        elif current is None or current["stance"] != item.answer or current["reason"] != item.note.strip():
            if current is not None and not current["uid"]:  # kept only on this machine: replace it there
                self._db.execute("UPDATE decisions SET status = 'superseded' WHERE id = ?", (current["id"],))
            if config.keep_local():
                # The developer asked for answers to stay on this machine: nothing is written to the repository.
                if current is not None and current["uid"]:
                    self._retire(current["uid"], "replaced by an answer kept on the author's machine")
                self._db.execute(
                    "INSERT INTO decisions (fingerprint, bug_class, stance, title, reason, file, symbol, content_hash,"
                    " commit_sha, created_at, confirmed_at, hash_v, given) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2, ?)",
                    (fp, bug_class, item.answer, finding.title, item.note.strip(), finding.file, symbol, content_hash,
                     commit, now, now, given))
            else:
                self._write_decision(finding, item, symbol, content_hash, given, current)
        outcome = "noise" if item.noise else (item.answer or "shown")
        self._db.execute("UPDATE ledger SET outcome = ? WHERE fingerprint = ?", (outcome, fp))
        self._db.commit()
        self.sync()

    def _forget_row(self, row: sqlite3.Row, reason: str) -> None:
        if row["uid"]:
            self._retire(row["uid"], reason)
        else:
            self._db.execute("UPDATE decisions SET status = 'forgotten' WHERE id = ?", (row["id"],))

    def resolve_conflict(self, decision_id: int, answer: str | None) -> None:
        """The author answered a "this goes against your earlier decision" question."""
        if answer == "keep":  # they changed their mind: the old decision no longer stands
            row = self._db.execute("SELECT id, uid FROM decisions WHERE id = ? AND status = 'active'",
                                   (decision_id,)).fetchone()
            if row is not None and row["uid"]:
                self._retire(row["uid"], "the author changed their mind")
            else:
                self._db.execute("UPDATE decisions SET status = 'superseded' WHERE id = ?", (decision_id,))
        elif answer == "change":  # a slip: the old decision stands and was just re-confirmed
            self._db.execute("UPDATE decisions SET confirmed_at = ? WHERE id = ?", (time.time(), decision_id))
        self._db.commit()
        self.sync()

    def record_shown(self, findings: list[Finding], commit: str = "") -> None:
        now = time.time()
        for f in findings:
            if f.kind == "background":
                continue
            symbol, _ = _anchor(self.repo, f.file, f.line)
            self._db.execute(
                "INSERT INTO ledger (fingerprint, title, kind, commit_sha, created_at) VALUES (?, ?, ?, ?, ?)",
                (fingerprint(f, symbol), f.title, f.kind, commit, now),
            )
        self._db.commit()

    def forget(self, kind: str, record_id: int, reason: str = "") -> bool:
        """Retire a decision, or drop a noise rule. A committed decision is retired by a new file."""
        if kind == "decision":
            row = self._db.execute("SELECT id, uid FROM decisions WHERE id = ? AND status = 'active'",
                                   (record_id,)).fetchone()
            if row is None:
                return False
            self._forget_row(row, reason or "retired by the author")
            self._db.commit()
            self.sync()
            return True
        if kind != "noise":
            return False
        changed = self._db.execute("UPDATE noise SET status = 'forgotten' WHERE id = ?", (record_id,)).rowcount
        self._db.commit()
        return bool(changed)

    # --- reading -----------------------------------------------------------

    def decisions(self, status: str = "active") -> list[Decision]:
        rows = self._db.execute("SELECT * FROM decisions WHERE status = ? ORDER BY created_at DESC", (status,))
        return [Decision(**{k: row[k] for k in Decision.__dataclass_fields__}) for row in rows]

    def noise_rules(self) -> list[sqlite3.Row]:
        return list(self._db.execute("SELECT * FROM noise WHERE status = 'active' ORDER BY created_at DESC"))

    def locate(self, d: Decision, also_in: list[str] | None = None) -> Located:
        """Find a decision's code as it is now. Reads only; never changes the record.

        In order: where it was; the same file under another name for the function; the file git
        says it was renamed to; another file in this change holding the very same code.
        """
        if self._renamed is None:
            self._renamed = gitops.renames(self.repo)
        places = [d.file]
        if d.file in self._renamed:
            places.append(self._renamed[d.file])
        for n, file in enumerate(places):
            source = _read(self.repo, file)
            if source is None:
                continue
            how = "moved" if n else ""
            if not d.symbol:  # a language without definitions: the decision is about the file
                return Located("fresh", file, "", 1, "", how)
            found = symbols.find(file, source, d.symbol)
            if found is not None:
                return Located(self._state(d, source, found), file, found.name, found.first, found.fingerprint, how)
            same_code = symbols.find_by_fingerprint(file, source, d.content_hash) if d.hash_v >= 2 else None
            if same_code is not None:
                return Located("fresh", file, same_code.name, same_code.first, same_code.fingerprint, how or "renamed")
        if d.symbol and d.hash_v >= 2:
            for file in also_in or []:
                source = _read(self.repo, file) if file not in places else None
                same_code = symbols.find_by_fingerprint(file, source, d.content_hash) if source else None
                if same_code is not None:
                    return Located("fresh", file, same_code.name, same_code.first, same_code.fingerprint, "moved")
        return Located("orphaned")

    def _state(self, d: Decision, source: str, found: symbols.Definition) -> str:
        if d.hash_v >= 2:
            now = found.fingerprint
        else:
            now = _text_hash("\n".join(source.splitlines()[found.first - 1 : found.last]))
        if now == d.content_hash:
            return "fresh"
        checked = self._db.execute("SELECT 1 FROM verified WHERE decision_id = ? AND fingerprint = ?",
                                   (d.id, found.fingerprint)).fetchone()
        return "fresh" if checked else "drifted"  # changed, but already judged to still honour the decision

    def mark_checked(self, decision_id: int, fingerprint: str) -> None:
        """Note that this version of the code was judged to still honour the decision."""
        self._db.execute("INSERT OR IGNORE INTO verified (decision_id, fingerprint, at) VALUES (?, ?, ?)",
                         (decision_id, fingerprint, time.time()))
        self._db.commit()

    def located(self, also_in: list[str] | None = None) -> list[tuple[Decision, Located]]:
        return [(d, self.locate(d, also_in)) for d in self.decisions()]

    def orphaned(self) -> list[Decision]:
        """Decisions whose code cannot be found. Shown until someone retires or re-homes them."""
        return [d for d, where in self.located() if where.state == "orphaned"]

    def touched(self, files: list[str]) -> list[tuple[Decision, Located]]:
        """Decisions whose function is in these files and no longer reads the way it did when decided."""
        return [(d, where) for d, where in self.located(files)
                if where.state == "drifted" and where.file in files]

    def context(self, files: list[str], limit: int = 12) -> str:
        """Decisions about the files in this change, for the reviewing models to respect."""
        relevant = [(d, where) for d, where in self.located(files)
                    if where.file in files and d.stance != "unsure" and not d.suspect][:limit]
        if not relevant:
            return ""
        lines = [f"- {where.file} ({where.symbol or 'file'}), decided {d.day}. {d.rule}" for d, where in relevant]
        return (
            "## Decisions the author already made about this code\n"
            "Do not raise these again as open questions. Do raise it if the new code goes against one.\n"
            + "\n".join(lines)
        )

    # --- applying memory to a finished review ---------------------------------

    async def apply(self, findings: list[Finding], changed_files: list[str], diff_of: Callable[[str], str],
                    same: Same, judge: Judge, max_checks: int = 5) -> list[str]:
        """Use memory on a review's findings. Returns plain notes on what memory did."""
        notes: list[str] = []
        lost = len(self.orphaned())
        if lost:
            notes.append(f"{lost} earlier decision{'s' if lost != 1 else ''} could not be matched to any code; "
                         "see `dowel memory`")

        # 1. Has this already been answered, or been asked to stop?
        for f in findings:
            if f.kind == "background":
                continue
            symbol, _ = _anchor(self.repo, f.file, f.line)
            fp = fingerprint(f, symbol)
            settled = [d for d in self.decisions() if d.stance in ("keep", "change")]
            earlier = next((d for d in settled if d.fingerprint == fp), None)
            p = await same(earlier.title, f.title) if earlier else None
            if earlier is None or (p is not None and p < SAME_BAR):
                # The same thing can surface in a different file next time (at a caller, say), so
                # also compare against decisions about the files in this change, at a higher bar.
                earlier = None
                near = [d for d in settled if d.fingerprint != fp and self.locate(d, changed_files).file in changed_files]
                for d in near[:12]:
                    p = await same(d.title, f.title)
                    if p is not None and p >= SAME_ELSEWHERE_BAR:
                        earlier = d
                        break
            # Memory only acts when the fast model has positively confirmed the match. If it is
            # unavailable, nothing is held back: asking twice is cheaper than hiding something.
            if earlier is not None and p is not None and p >= SAME_BAR:  # noqa: SIM102 - rule, then case
                if earlier.stance in ("keep", "change"):
                    if earlier.stance == "keep" and (f.kind == "issue" or f.hypothesis.bug_class in NEVER_SUPPRESS):
                        # "Keep it" answered a question about what the author wants. A concrete
                        # failing path found since then is new information, so it is shown.
                        f.memory = f"you chose to keep this behaviour on {earlier.day}; Dowel now sees it fail"
                    elif earlier.stance == "keep":
                        f.kind, f.memory = "background", f"you decided to keep this on {earlier.day}"
                        notes.append(f"not asked again: {f.title}")
                    else:
                        f.kind, f.priority = "issue", "P1"
                        f.memory = f"on {earlier.day} you decided to change this, and it is still here"
                    continue
            if f.hypothesis.bug_class in NEVER_SUPPRESS or (f.kind == "issue" and f.priority == "P1"):
                continue
            for rule in self.noise_rules():
                if rule["bug_class"] != f.hypothesis.bug_class or rule["scope"] != f.file:
                    continue
                p = await same(rule["example"], f.title)
                if p is not None and p >= SAME_BAR:
                    f.kind, f.memory = "background", "you asked Dowel to stop raising this kind of thing here"
                    self._db.execute("UPDATE noise SET hits = hits + 1 WHERE id = ?", (rule["id"],))
                    notes.append(f"held back as noise: {f.title}")
                    break

        # 2. Does this change go against an earlier decision?
        for recorded, where in self.touched(changed_files)[:max_checks]:
            if recorded.stance not in ("keep", "change"):
                continue
            d = replace(recorded, file=where.file, symbol=where.symbol)  # where the code is now
            ruling = await judge(d, diff_of(d.file))
            if ruling is None:
                continue
            if not ruling.contradicts:
                # The function changed but still honours the decision. Note that this version was
                # checked, so it is not judged again; the decision itself is left as it was made.
                self.mark_checked(d.id, where.fingerprint)
                continue
            findings.insert(0, _memory_finding(d, ruling))
            notes.append(f"goes against a decision from {d.day}: {d.title}")
        self._db.commit()
        return notes


def _memory_finding(d: Decision, ruling: Contradiction) -> Finding:
    was = "keep this" if d.stance == "keep" else "change this"
    reason = f' Your reason then: "{d.reason}".' if d.reason else ""
    hypothesis = Hypothesis(id=f"m{d.id}", statement=ruling.explanation, bug_class="other", file=d.file,
                            line=ruling.line, check="compare with the earlier decision")
    verdict = Verdict(status="unresolved", reasoning=ruling.explanation, trigger="", question_for_author="",
                      evidence=[Evidence(file=d.file, start_line=ruling.line, end_line=ruling.line, quote="")]
                      if ruling.line else [])
    return Finding(
        id=f"m{d.id}", kind="decision", priority="P1", file=d.file, line=ruling.line or 1,
        title=ruling.explanation,
        detail=f"On {d.day} you decided to {was}: {d.title.rstrip('.')}.{reason}",
        question="Have you changed your mind, or is this a slip?",
        hypothesis=hypothesis, verdict=verdict, memory=f"goes against your decision of {d.day}",
    )


def record_stated(repo: Path, file: str, line: int, title: str, stance: str, reason: str, given: str) -> None:
    """Record a decision that did not come out of a review: one a person said, or one taken from history."""
    hypothesis = Hypothesis(id="s1", statement=title, bug_class="other", file=file, line=line, check="-")
    verdict = Verdict(status="confirmed", reasoning="-", trigger="-", question_for_author="",
                      evidence=[Evidence(file=file, start_line=line, end_line=line, quote="-")])
    finding = Finding(id="s1", kind="decision", title=title, file=file, line=line, hypothesis=hypothesis, verdict=verdict)
    memory = Memory(repo)
    try:
        memory.remember_answer(finding, ItemState(answer=stance, note=reason), given=given)  # type: ignore[arg-type]
    finally:
        memory.close()


def remember(repo: str, run: RunResult, finding_id: str, item: ItemState, given: str = "") -> None:
    """Called whenever the author answers or silences a finding, from the page or the walkthrough."""
    finding = next((f for f in run.findings if f.id == finding_id), None)
    if finding is None:
        return
    memory = Memory(repo)
    try:
        if finding_id.startswith("m") and finding_id[1:].isdigit():
            memory.resolve_conflict(int(finding_id[1:]), item.answer)
        else:
            memory.remember_answer(finding, item, given=given)
    finally:
        memory.close()
