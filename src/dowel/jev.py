"""Typesafe Jev: a fast decision model for closed questions.

Jev does not write text. It answers yes/no ("noul") and pick-one ("choice") questions with a
probability. Dowel only asks it narrow, per-claim questions; anything below the confidence bar,
and any failure, falls back to Claude's own ruling.
"""

from __future__ import annotations

import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

from dowel.config import Settings
from dowel.schemas import Usage


@dataclass(frozen=True)
class Answer:
    value: float | str
    confidence: float | None


@dataclass
class Check:
    """One closed question put to the fast model, kept so a run can show what it asked and how fast."""

    name: str
    question: str
    answer: str
    probability: float | None
    ms: int
    acted: bool = False  # whether the engine changed something because of this answer


@dataclass
class CheckLog:
    checks: list[Check] = field(default_factory=list)

    def summary(self) -> dict[str, float | int]:
        times = [c.ms for c in self.checks]
        return {
            "count": len(times),
            "acted": sum(c.acted for c in self.checks),
            "median_ms": int(statistics.median(times)) if times else 0,
            "p90_ms": int(sorted(times)[int(len(times) * 0.9) - 1]) if len(times) >= 10 else (max(times) if times else 0),
        }


class Jev:
    def __init__(self, settings: Settings, usage: Usage) -> None:
        self._settings = settings
        self._usage = usage
        self._client = httpx.AsyncClient(timeout=10.0)
        self._disabled_reason: str | None = None if settings.typesafe_key else "no TYPESAFE_API_KEY set"
        self._warned = False
        self.log = CheckLog()

    @property
    def available(self) -> bool:
        return self._disabled_reason is None

    def _disable(self, reason: str) -> None:
        # Fail loudly once: an earlier project lost a week of Jev checks to a silent fallback.
        self._disabled_reason = reason
        if not self._warned:
            self._warned = True
            print(f"dowel: Typesafe checks are off for this run ({reason}); Claude decides instead.",
                  file=sys.stderr)

    async def ask(self, state: str, questions: dict[str, dict]) -> dict[str, Answer] | None:
        """Ask several questions about one piece of text. None means 'no answer, use the fallback'."""
        if not self.available:
            return None
        try:
            reply = await self._client.post(
                f"{self._settings.typesafe_base_url}/v1/systemone",
                headers={"Authorization": f"Bearer {self._settings.typesafe_key}"},
                json={"model": self._settings.models.jev, "state": state, "questions": questions},
            )
        except httpx.HTTPError as exc:
            self._disable(f"could not reach Typesafe: {type(exc).__name__}")
            return None
        if reply.status_code in (401, 402, 403):
            self._disable(f"Typesafe returned {reply.status_code} (key or credit problem)")
            return None
        if reply.status_code != 200:
            return None
        self._usage.jev_calls += 1
        try:
            answers = reply.json().get("answers", {})
            out: dict[str, Answer] = {}
            for name, q in questions.items():
                raw = answers.get(name) or {}
                value = raw.get(q["type"])
                if value is None:
                    return None
                out[name] = Answer(
                    value=str(value) if q["type"] == "choice" else float(value),
                    confidence=raw.get("confidence"),
                )
        except (ValueError, TypeError, AttributeError, KeyError):
            return None  # a reply in a shape we do not expect: fall back, as promised above
        return out

    async def yes(self, state: str, statement: str, name: str = "check") -> float | None:
        """Probability that `statement` is true of `state`."""
        started = time.monotonic()
        got = await self.ask(state, {"q": {"type": "noul", "instructions": statement}})
        if got is None:
            return None
        p = float(got["q"].value)
        self.log.checks.append(Check(name, statement[:160], f"{p:.2f}", p, int((time.monotonic() - started) * 1000)))
        return p

    async def choose(self, state: str, instructions: str, options: dict[str, str],
                     name: str = "choice") -> tuple[str, float] | None:
        """Pick one of `options` (label -> description). Returns the label and the model's confidence."""
        started = time.monotonic()
        got = await self.ask(state, {"q": {"type": "choice", "instructions": instructions, "criteria": options}})
        if got is None or got["q"].value not in options:
            return None
        label, confidence = str(got["q"].value), float(got["q"].confidence or 0.0)
        self.log.checks.append(Check(name, instructions[:160], label, confidence, int((time.monotonic() - started) * 1000)))
        return label, confidence

    def acted(self) -> None:
        """Mark the most recent check as one the engine acted on."""
        if self.log.checks:
            self.log.checks[-1].acted = True

    async def aclose(self) -> None:
        await self._client.aclose()
