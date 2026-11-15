"""Memory: decisions are tied to code, expire when it changes or goes, and never hide what matters."""

import asyncio
from pathlib import Path

import pytest

from dowel.memory import Contradiction, Memory
from dowel.schemas import Evidence, Finding, Hypothesis, Verdict
from dowel.store import ItemState

LOADER = "def load_config(path):\n    if not path.exists():\n        return DEFAULTS\n    return read(path)\n\n\ndef other():\n    return 1\n"


def finding(fid: str = "h1", kind: str = "decision", bug_class: str = "config_or_default", line: int = 3,
            title: str = "A missing config file now falls back to defaults.", priority: str = "P2") -> Finding:
    h = Hypothesis(id=fid, statement=title, bug_class=bug_class, file="loader.py", line=line, check="look")
    v = Verdict(status="confirmed", reasoning="r", trigger="t", question_for_author="",
                evidence=[Evidence(file="loader.py", start_line=line, end_line=line, quote="x")])
    return Finding(id=fid, kind=kind, priority=priority, title=title, file="loader.py", line=line, hypothesis=h, verdict=v)


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    (tmp_path / "loader.py").write_text(LOADER)
    return Memory(tmp_path, db_path=tmp_path / "memory.db")


async def always_same(_a: str, _b: str) -> float:
    return 0.95


async def never_judged(_decision, _diff) -> None:
    raise AssertionError("no decision should have been checked")


def apply(memory: Memory, findings: list[Finding], judge=never_judged, same=always_same, files=("loader.py",)) -> list[str]:
    return asyncio.run(memory.apply(findings, list(files), lambda _p: "diff", same, judge))


def test_an_answer_is_tied_to_its_function_and_updated_in_place(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(answer="keep", note="staging has no config"))
    memory.remember_answer(finding(), ItemState(answer="keep", note="staging and CI have no config"))
    (only,) = memory.decisions()
    assert (only.symbol, only.stance, only.reason) == ("load_config", "keep", "staging and CI have no config")

    memory.remember_answer(finding(), ItemState(answer="change", note="must fail"))
    assert [d.stance for d in memory.decisions()] == ["change"]
    # Nothing was committed yet, so the one draft file was rewritten, not piled up.
    assert len(list((memory.repo / ".dowel/decisions").glob("*.md"))) == 1


def test_a_kept_decision_is_not_asked_again(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(answer="keep"))
    again = finding()
    notes = apply(memory, [again])
    assert again.kind == "background" and "decided to keep" in again.memory
    assert notes == [f"not asked again: {again.title}"]


def test_a_change_decision_that_is_still_there_becomes_a_top_issue(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(answer="change"))
    again = finding()
    apply(memory, [again])
    assert (again.kind, again.priority) == ("issue", "P1") and "still here" in again.memory


def test_noise_rules_hold_back_minor_items_but_never_security_or_top_issues(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(noise=True))
    minor = finding(fid="h2", line=8)
    security = finding(fid="h3", bug_class="access_or_security", line=8)
    memory.remember_answer(security, ItemState(noise=True))
    urgent = finding(fid="h4", kind="issue", priority="P1", line=8)
    apply(memory, [minor, security, urgent])
    assert minor.kind == "background" and "stop raising" in minor.memory
    assert security.kind == "decision" and urgent.kind == "issue"
    assert memory.noise_rules()[-1]["hits"] == 1


def test_a_different_finding_in_the_same_place_is_still_shown(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(answer="keep"))

    async def different(_a: str, _b: str) -> float:
        return 0.1

    other = finding(title="The defaults turn signature checking off.")
    apply(memory, [other], same=different)
    assert other.kind == "decision" and other.memory == ""


def test_a_decision_whose_code_is_gone_is_shown_as_orphaned_and_comes_back_with_the_code(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(answer="keep"))
    (memory.repo / "loader.py").write_text("def other():\n    return 1\n")  # as on another branch
    notes = apply(memory, [])
    assert "could not be matched" in notes[0]
    assert [d.id for d in memory.orphaned()] == [1]
    assert len(memory.decisions()) == 1  # reading changed nothing

    (memory.repo / "loader.py").write_text(LOADER)  # back on the branch where it was decided
    assert memory.orphaned() == [] and memory.locate(memory.decisions()[0]).state == "fresh"


def test_code_that_goes_against_a_decision_is_raised_with_the_old_reason(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(answer="change", note="a missing config must stop the app"))
    (memory.repo / "loader.py").write_text(LOADER.replace("return DEFAULTS", "log('missing')\n        return DEFAULTS"))

    async def judge(decision, _diff) -> Contradiction:
        assert decision.symbol == "load_config"
        return Contradiction(contradicts=True, line=4, explanation="A missing config file still starts the app on defaults.")

    findings: list[Finding] = []
    notes = apply(memory, findings, judge=judge)
    (raised,) = findings
    assert raised.id.startswith("m") and raised.kind == "decision" and raised.priority == "P1"
    assert "a missing config must stop the app" in raised.detail and "changed your mind" in raised.question
    assert notes[0].startswith("goes against a decision")


def test_comments_and_layout_do_not_count_as_a_change(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(answer="keep"))
    (memory.repo / "loader.py").write_text(
        LOADER.replace("return read(path)", "# why: staging\n    return read( path )  # same")
    )
    assert apply(memory, []) == []  # never_judged would raise if a model had been asked
    assert memory.locate(memory.decisions()[0]).state == "fresh"


def test_a_harmless_edit_is_judged_once_and_the_decision_is_left_as_made(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(answer="keep"))
    before = memory.decisions()[0]
    (memory.repo / "loader.py").write_text(LOADER.replace("return read(path)", "return read(path, strict=True)"))
    asked = []

    async def judge(decision, _diff) -> Contradiction:
        asked.append(decision.id)
        return Contradiction(contradicts=False, line=0, explanation="unchanged behaviour")

    findings: list[Finding] = []
    assert apply(memory, findings, judge=judge) == [] and findings == []
    assert apply(memory, findings, judge=judge) == []
    assert asked == [1]  # the second review does not pay to ask again about the same code
    assert memory.decisions()[0] == before and memory.touched(["loader.py"]) == []


def test_a_decision_follows_a_renamed_function(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(answer="keep"))
    (memory.repo / "loader.py").write_text(LOADER.replace("def load_config", "def load_settings"))
    where = memory.locate(memory.decisions()[0])
    assert (where.state, where.symbol, where.how) == ("fresh", "load_settings", "renamed")
    (memory.repo / "loader.py").write_text(LOADER.replace("def other", "def unrelated"))
    assert memory.locate(memory.decisions()[0]).how == ""  # its own name did not change

    (memory.repo / "extra.py").write_text(LOADER.split("\n\n\n")[0] + "\n")
    (memory.repo / "loader.py").write_text("def other():\n    return 1\n")
    where = memory.locate(memory.decisions()[0], also_in=["extra.py"])
    assert (where.state, where.file, where.symbol, where.how) == ("fresh", "extra.py", "load_config", "moved")


def test_a_decision_follows_a_file_git_renamed(tmp_path: Path) -> None:
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "loader.py").write_text(LOADER)
    git("add", "."), git("commit", "-qm", "one")
    memory = Memory(tmp_path, db_path=tmp_path / "memory.db")
    memory.remember_answer(finding(), ItemState(answer="keep"))
    git("mv", "loader.py", "config_loader.py"), git("commit", "-qm", "rename")

    where = Memory(tmp_path, db_path=tmp_path / "memory.db").locate(memory.decisions()[0])
    assert (where.state, where.file, where.how) == ("fresh", "config_loader.py", "moved")


def test_methods_with_the_same_name_in_two_classes_are_different_decisions(tmp_path: Path) -> None:
    source = "class A:\n    def load(self):\n        return 1\n\n\nclass B:\n    def load(self):\n        return 2\n"
    (tmp_path / "loader.py").write_text(source)
    memory = Memory(tmp_path, db_path=tmp_path / "memory.db")
    memory.remember_answer(finding(line=7), ItemState(answer="keep"))
    (only,) = memory.decisions()
    assert only.symbol == "B.load"

    (tmp_path / "loader.py").write_text(source.replace("return 1", "return 100"))  # A changed, B did not
    assert memory.locate(only).state == "fresh"
    (tmp_path / "loader.py").write_text(source.replace("return 2", "return 200"))
    assert memory.locate(only).state == "drifted"


def test_changing_your_mind_retires_the_old_decision(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(answer="change"))
    decision_id = memory.decisions()[0].id
    memory.resolve_conflict(decision_id, "keep")
    assert memory.decisions() == []


def test_forget_and_context(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(answer="keep", note="on purpose"))
    assert "Intended: A missing config file now falls back to defaults. (on purpose)" in memory.context(["loader.py"])
    assert memory.context(["elsewhere.py"]) == ""
    assert memory.forget("decision", memory.decisions()[0].id) and memory.decisions() == []


def test_the_same_thing_raised_from_another_file_is_still_recognised(memory: Memory) -> None:
    (memory.repo / "job.py").write_text("def run():\n    return load_config(1)\n")
    memory.remember_answer(finding(), ItemState(answer="change", note="must stop the app"))
    h = Hypothesis(id="h9", statement="s", bug_class="scope_beyond_intent", file="job.py", line=2, check="c")
    v = Verdict(status="confirmed", reasoning="r", trigger="t", question_for_author="", evidence=[])
    elsewhere = Finding(id="h9", kind="decision", title="A missing config file now loads defaults instead of skipping the job.",
                        file="job.py", line=2, hypothesis=h, verdict=v)
    apply(memory, [elsewhere], files=("loader.py", "job.py"))
    assert (elsewhere.kind, elsewhere.priority) == ("issue", "P1") and "still here" in elsewhere.memory

    async def unsure(_a: str, _b: str) -> float:
        return 0.75  # enough for the same function, not enough across files

    cautious = Finding(id="h9", kind="decision", title="Something else in the job.", file="job.py", line=2, hypothesis=h, verdict=v)
    apply(memory, [cautious], same=unsure, files=("loader.py", "job.py"))
    assert cautious.kind == "decision" and cautious.memory == ""


def test_memory_never_hides_an_issue_or_acts_without_confirmation(memory: Memory) -> None:
    memory.remember_answer(finding(), ItemState(answer="keep"))
    now_failing = finding(kind="issue")
    apply(memory, [now_failing])
    assert now_failing.kind == "issue" and "now sees it fail" in now_failing.memory

    async def unavailable(_a: str, _b: str) -> None:
        return None

    asked_again = finding()
    apply(memory, [asked_again], same=unavailable)
    assert asked_again.kind == "decision" and asked_again.memory == ""
