"""What happens between hearing the developer and acting on it.

Two things run here, both built so they can be tested without a call:

- `hear` asks the fast decision model what a spoken reply was: an answer (keep, change, not sure),
  a question about the code, a correction of an earlier answer, or something else. A confident
  answer is recorded straight away, in the developer's own words, without waiting for the talking
  model to decide to call a tool.
- `follow_up` takes an answer to a question the review could not settle from the code alone, and
  re-checks that one suspicion with the answer as a fact. A maybe can become a confirmed issue
  while the conversation carries on.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from dowel import prompts
from dowel.schemas import Evidence, Finding, Strict
from dowel.tools import TOOLS

Intent = Literal["keep", "change", "unsure", "question", "correction", "other"]
ANSWER_BAR = 0.8  # the fast model's confidence before a spoken answer is recorded without asking again

_OPTIONS: dict[str, str] = {
    "keep": "They want the behaviour that was just described to stay as it is, or say an issue is fine to leave.",
    "change": "They do not want that behaviour, or want the issue fixed.",
    "unsure": "They do not know, say the agent wrote it, or want a reviewer to look.",
    "question": "They are asking something about the code or the change.",
    "correction": "They are taking back or changing an answer they gave a moment ago.",
    "other": "Anything else: small talk, thinking aloud, asking to skip, pause or repeat.",
}

Choose = Callable[[str, str, dict[str, str]], Awaitable[tuple[str, float] | None]]


@dataclass(frozen=True)
class Heard:
    intent: Intent
    confidence: float

    @property
    def is_answer(self) -> bool:
        return self.intent in ("keep", "change", "unsure") and self.confidence >= ANSWER_BAR


async def hear(choose: Choose, asked: str, said: str) -> Heard | None:
    """Classify what the developer just said, given what Dowel had asked."""
    if len(said.split()) < 1:
        return None
    got = await choose(
        f"Dowel explained a code change and asked: {asked}\n\nThe developer replied: {said}",
        "What did the developer do with this reply?",
        _OPTIONS,
    )
    if got is None:
        return None
    return Heard(intent=got[0], confidence=round(got[1], 2))  # type: ignore[arg-type]


_YES = re.compile(r"\b(yes|yeah|yep|allow|go ahead|go for it|fine|okay|ok|sure|do it|let it|haan|theek|kar do)\b", re.I)
_NO = re.compile(r"\b(no|nope|don't|do not|refuse|stop|deny|leave it|keep it|not allowed|nahi|mat|rehne do)\b", re.I)


async def allow_or_refuse(fast: Any, decision: str, said: str) -> bool | None:
    """Did they allow the agent's change or refuse it? None if they did neither clearly.

    The fast model decides when it is there. Without it, plain words do: a clear yes or a clear no.
    A reply with both, or neither, is asked again and never guessed.
    """
    if fast is not None:
        got = await fast.choose(
            f'A coding agent wants to change code under this decision: "{decision}"\nThe developer was asked '
            f"whether to allow the change and replied: {said}",
            "What did the developer say about the agent's change?",
            {"allow": "They allow the agent to make the change.", "refuse": "They do not allow it, or want the "
             "code kept as it is.", "unclear": "They did not clearly allow or refuse, or asked something."},
            name="allow the agent's change")
        if got is not None and got[1] >= ANSWER_BAR and got[0] in ("allow", "refuse"):
            return got[0] == "allow"
        if got is not None:
            return None
    yes, no = bool(_YES.search(said)), bool(_NO.search(said))
    return yes if yes != no else None


class FollowUp(Strict):
    status: Literal["confirmed", "refuted", "unresolved"] = Field(
        description="confirmed: given the author's answer, this is now a real problem in the code."
    )
    title: str = Field(description="What goes wrong, in at most 16 plain words. Empty unless confirmed.")
    reasoning: str = Field(description="Two sentences: how the answer settles it, and what the code does.")
    evidence: list[Evidence]


_FOLLOW_UP = prompts.INVESTIGATE + """

This suspicion could not be settled from the code alone, so the author was asked. Their answer is \
a fact you can now rely on. Decide again with it. Confirmed means: given what they said, the code \
as written does the wrong thing, and you can quote the lines."""


async def follow_up(claude: Any, model: str, run_tool: Callable[..., Awaitable[str]], finding: Finding,
                    answer: str, said: str) -> FollowUp | None:
    """Re-check one unsettled suspicion now that the author has answered. None if it could not run."""
    try:
        return await claude.agent(
            model=model,
            system=_FOLLOW_UP,
            user=(
                f"Suspicion at {finding.file}:{finding.line}: {finding.hypothesis.statement}\n\n"
                f"Why it was unsettled: {finding.verdict.reasoning}\n\n"
                f"The author was asked: {finding.question or finding.verdict.question_for_author}\n"
                f"The author answered ({answer}): {said}"
            ),
            output=FollowUp,
            tools=TOOLS,
            run_tool=run_tool,
            max_tool_calls=4,
            effort="low",
            max_tokens=3000,
        )
    except Exception:  # noqa: BLE001 - a follow-up is a bonus; it must never disturb the conversation
        return None


def apply_follow_up(finding: Finding, result: FollowUp) -> bool:
    """Turn a confirmed follow-up into an issue on the finding. Returns whether it changed."""
    if result.status != "confirmed" or not result.title.strip():
        return False
    finding.kind, finding.priority = "issue", "P1"
    finding.title = result.title.strip()
    finding.detail = result.reasoning
    finding.question = ""
    finding.memory = "confirmed after your answer"
    if result.evidence:
        finding.verdict.evidence = result.evidence
        finding.file, finding.line = result.evidence[0].file, result.evidence[0].start_line
    return True
