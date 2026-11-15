"""The live loop: what a spoken reply was, and an answer settling what the code could not."""

import asyncio

from dowel.schemas import Evidence, Finding, Hypothesis, Verdict
from dowel.voice import live


def run(coro):
    return asyncio.run(coro)


def chooser(label: str, confidence: float):
    async def choose(_state: str, _ask: str, options: dict[str, str]):
        assert label in options
        return label, confidence
    return choose


def test_a_confident_spoken_answer_counts_and_an_unsure_one_does_not() -> None:
    sure = run(live.hear(chooser("change", 0.93), "Keep that?", "No, a missing config has to stop the app."))
    assert sure.is_answer and sure.intent == "change"
    vague = run(live.hear(chooser("keep", 0.55), "Keep that?", "Hmm, probably, I think"))
    assert not vague.is_answer
    asking = run(live.hear(chooser("question", 0.97), "Keep that?", "Wait, who else calls this?"))
    assert not asking.is_answer and asking.intent == "question"


def unsettled() -> Finding:
    h = Hypothesis(id="h4", statement="Retries can send the same event twice.", bug_class="retry_or_partial_failure",
                   file="deliver.py", line=30, check="look for a delivery id")
    v = Verdict(status="unresolved", reasoning="Depends on whether receivers cope with duplicates.", trigger="",
                question_for_author="Can receivers cope with the same event twice?", evidence=[])
    return Finding(id="h4", kind="decision", title=h.statement, file="deliver.py", line=30, hypothesis=h, verdict=v,
                   question="Can receivers cope with the same event twice?")


class Reader:
    def __init__(self, result=None, fail: bool = False) -> None:
        self.result, self.fail, self.user = result, fail, ""

    async def agent(self, *, user: str, **_):
        self.user = user
        if self.fail:
            raise RuntimeError("model unavailable")
        return self.result


async def no_tool(*_args) -> str:
    return ""


def test_an_answer_can_turn_a_maybe_into_a_confirmed_issue() -> None:
    finding = unsettled()
    confirmed = live.FollowUp(status="confirmed", title="Retries can double-charge: no delivery id is sent.",
                              reasoning="The author says receivers cannot dedupe. The same body is posted again.",
                              evidence=[Evidence(file="deliver.py", start_line=33, end_line=33, quote="httpx.post(...)")])
    reader = Reader(confirmed)
    result = run(live.follow_up(reader, "m", no_tool, finding, "change", "No, they'd double-charge."))
    assert "No, they'd double-charge." in reader.user and "Can receivers cope" in reader.user
    assert live.apply_follow_up(finding, result)
    assert (finding.kind, finding.priority, finding.line) == ("issue", "P1", 33)
    assert finding.memory == "confirmed after your answer" and finding.question == ""


def test_a_follow_up_that_fails_or_refutes_changes_nothing() -> None:
    finding = unsettled()
    assert run(live.follow_up(Reader(fail=True), "m", no_tool, finding, "keep", "They dedupe.")) is None
    refuted = live.FollowUp(status="refuted", title="", reasoning="Receivers dedupe.", evidence=[])
    assert not live.apply_follow_up(finding, refuted) and finding.kind == "decision"
