"""Before a question reaches the developer, check whether they already answered it.

People tell their coding agent what they want. If a review then asks them the same thing, the
tool has wasted their time. When the hook has recorded the developer's prompt, each question is
compared with it by the fast decision model. A question the prompt already answers is not
dropped, because the match can be wrong: it is marked, with the sentence from the prompt that
seems to answer it, so the developer confirms instead of answering from scratch.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from dowel.schemas import Finding

BAR = 0.8
Yes = Callable[[str, str], Awaitable[float | None]]


def _closest_sentence(prompt: str, about: str) -> str:
    words = set(re.findall(r"[a-z]{4,}", about.lower()))
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", prompt) if s.strip()]
    best = max(sentences, key=lambda s: len(words & set(re.findall(r"[a-z]{4,}", s.lower()))), default="")
    return best[:240]


async def mark_answered(findings: list[Finding], prompt: str, yes: Yes) -> list[str]:
    """Mark questions the developer's own prompt already answers. Returns notes on what was marked."""
    notes: list[str] = []
    if not prompt.strip():
        return notes
    for f in findings:
        if f.kind != "decision" or f.memory:
            continue
        p = await yes(
            f"What the developer told their coding agent:\n{prompt[:6000]}\n\n"
            f"A question a reviewer now wants to ask them: {f.question or f.title}",
            "What the developer told the agent already answers this question.",
        )
        if p is None or p < BAR:
            continue
        said = _closest_sentence(prompt, f"{f.title} {f.question}")
        f.memory = "your prompt may already answer this"
        f.detail = (f'From your prompt: "{said}" ' if said else "") + f.detail
        f.jev["answered_in_prompt"] = round(p, 2)
        notes.append(f"your prompt may already answer: {f.title}")
    return notes
