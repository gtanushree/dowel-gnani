"""Typed outputs of every engine stage.

These are the contract between stages, the CLI, the proof sheet and the walkthrough.
Models sent to Claude as an output format forbid extra keys so the JSON schema is strict.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- intent ---------------------------------------------------------------


class Claim(Strict):
    """One thing the author says this change does (or deliberately does not do)."""

    id: str = Field(description="Short id like c1, c2.")
    text: str = Field(description="The claim in one plain sentence.")
    kind: Literal["does", "does_not"] = Field(description="'does_not' for stated non-goals.")


class IntentGuess(Strict):
    """What Dowel thinks the change is for. A guess the author confirms in the recap."""

    summary: str = Field(description="Two or three plain sentences on what the change does, by behaviour.")
    claims: list[Claim]
    confidence: Literal["low", "medium", "high"] = Field(
        description="How well the description and commits explain the diff."
    )


# --- hypotheses -----------------------------------------------------------

BugClass = Literal[
    "retry_or_partial_failure",
    "contract_or_caller",
    "error_path",
    "false_success",
    "concurrency",
    "access_or_security",
    "limits_or_truncation",
    "stale_state",
    "missing_behaviour",
    "data_or_migration",
    "hidden_from_view",
    "time_or_schedule",
    "config_or_default",
    "deploy_mismatch",
    "docs_contradiction",
    "test_cannot_fail",
    "scope_beyond_intent",
    "other",
]


class Hypothesis(Strict):
    """One concrete way this change might be wrong or surprising."""

    id: str = Field(description="Short id like h1, h2.")
    statement: str = Field(description="'X might break because Y', concrete and checkable.")
    bug_class: BugClass
    file: str = Field(description="Repo-relative path most relevant to the hypothesis.")
    line: int = Field(description="Line in the new version of the file, or 0 if unknown.")
    check: str = Field(description="What an investigator should look at to confirm or refute it.")


class HypothesisList(Strict):
    hypotheses: list[Hypothesis]


# --- investigation --------------------------------------------------------


class Evidence(Strict):
    file: str
    start_line: int
    end_line: int
    quote: str = Field(description="The exact line or lines that matter, copied verbatim.")


class Verdict(Strict):
    """An investigator's finding on one hypothesis."""

    status: Literal["confirmed", "refuted", "unresolved"] = Field(
        description=(
            "confirmed: the code shows the problem. refuted: the code shows it cannot happen. "
            "unresolved: it depends on something only the author knows."
        )
    )
    reasoning: str = Field(description="Two to four sentences on what was found.")
    evidence: list[Evidence]
    trigger: str = Field(
        description="For confirmed: the concrete input or situation that hits the problem. Otherwise empty."
    )
    question_for_author: str = Field(
        description=(
            "For unresolved: one plain question about what the author wants, phrased as behaviour. "
            "Otherwise empty."
        )
    )


FindingKind = Literal["issue", "decision", "background"]


class Review(Strict):
    """The verifier's independent ruling on a confirmed or unresolved verdict."""

    holds: bool = Field(description="False if the claim is wrong or has no concrete trigger path.")
    kind: FindingKind = Field(
        description=(
            "issue: there is a concrete failing path. decision: both behaviours are valid and only "
            "the author's intent decides. background: true but not worth raising."
        )
    )
    title: str = Field(description="One plain sentence stating what the code now does or what breaks.")
    refutation_attempt: str = Field(description="How you tried to prove the claim wrong, and the result.")
    question: str = Field(
        description="For decisions: 'X now happens instead of Y. Keep that?' style. Otherwise empty."
    )


class Entry(Strict):
    """One thing to show the author, after merging findings that share a root cause."""

    ids: list[str] = Field(description="Hypothesis ids covered by this entry. The first is the main one.")
    kind: FindingKind
    priority: Literal["P1", "P2"] = Field(description="P1: wrong results, lost or exposed data, or broken for real users. P2: real but limited.")
    title: str = Field(description="What the code now does or what breaks. One sentence, at most 16 words.")
    detail: str = Field(description="One sentence on the consequence or where it bites. May be empty.")
    question: str = Field(description="For decisions: a short question about what the author wants. Else empty.")


class Sorted(Strict):
    entries: list[Entry] = Field(description="Most important first.")


# --- results --------------------------------------------------------------


class Finding(BaseModel):
    id: str
    kind: FindingKind
    title: str
    file: str
    line: int
    hypothesis: Hypothesis
    verdict: Verdict
    review: Review | None = None
    question: str = ""
    detail: str = ""
    priority: str = "P2"
    memory: str = ""  # what Dowel remembered about this, if anything
    merged: list[str] = Field(default_factory=list)
    searched: list[str] = Field(default_factory=list)
    jev: dict[str, float] = Field(default_factory=dict)


class Dropped(BaseModel):
    hypothesis: Hypothesis
    reason: str
    stage: Literal["investigate", "evidence_gate", "verify", "duplicate"]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    jev_calls: int = 0
    by_model: dict[str, float] = Field(default_factory=dict)  # dollars spent, per model


class StageTiming(BaseModel):
    name: str
    detail: str
    seconds: float


class FastCheck(BaseModel):
    name: str
    question: str
    answer: str
    ms: int
    acted: bool = False


class RunResult(BaseModel):
    repo: str
    base: str
    head: str
    files_changed: int
    lines_changed: int = 0
    parts: int = 1
    skipped: list[str] = Field(default_factory=list)
    fast_checks: list[FastCheck] = Field(default_factory=list)
    fast_check_summary: dict[str, float] = Field(default_factory=dict)
    intent: IntentGuess
    findings: list[Finding]
    dropped: list[Dropped]
    stages: list[StageTiming]
    usage: Usage
    seconds: float

    @property
    def issues(self) -> list[Finding]:
        return [f for f in self.findings if f.kind == "issue"]

    @property
    def decisions(self) -> list[Finding]:
        return [f for f in self.findings if f.kind == "decision"]
