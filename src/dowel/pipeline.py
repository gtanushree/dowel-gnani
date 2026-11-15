"""The review engine: context, intent, hypotheses, investigation, verification, sorting."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dowel import asked, prompts, scope, screen, store
from dowel import context as context_pack
from dowel import plan as planning
from dowel.config import Settings
from dowel.gitops import Change, default_base, load_change, repo_root
from dowel.jev import Jev
from dowel.llm import Claude, CostCapReached, Ledger, LLMError
from dowel.memory import Contradiction, Decision, Memory
from dowel.schemas import (
    Dropped,
    Evidence,
    FastCheck,
    Finding,
    Hypothesis,
    HypothesisList,
    IntentGuess,
    Review,
    RunResult,
    Sorted,
    StageTiming,
    Verdict,
)
from dowel.tools import TOOLS, RepoTools, describe_call


@dataclass(frozen=True)
class Event:
    kind: str  # "stage" | "finding" | "note"
    text: str
    seconds: float = 0.0
    finding: Finding | None = None


Emit = Callable[[Event], None]


def _shared_context(change: Change, review_plan: planning.ReviewPlan, part: planning.Part, pack_text: str,
                    settings: Settings) -> list[dict[str, Any]]:
    whole = ""
    if len(review_plan.parts) > 1:
        whole = (
            f"<whole_change>\nThis is one part of a larger change of {review_plan.lines_changed} lines. "
            f"All files in the change:\n{review_plan.overview}\n</whole_change>\n\n"
        )
    body = (
        f"<description>\n{change.description[:6000] or '(no commit messages yet)'}\n</description>\n\n{whole}"
        f"<diff>\n{part.diff(settings.budgets.diff_chars)}\n</diff>\n\n<context>\n{pack_text}\n</context>"
    )
    # Cached so the many parallel investigators share one copy of the change.
    return [{"type": "text", "text": body, "cache_control": {"type": "ephemeral"}}]


def _focused_context(change: Change, review_plan: planning.ReviewPlan, h: Hypothesis,
                     settings: Settings) -> list[dict[str, Any]]:
    """What one investigator starts with: the change to its own file. It has tools for the rest.

    Handing every investigator the whole part made a large review cost several times more, because
    the same tens of thousands of tokens were re-read on each of its steps.
    """
    own = next((f for part in review_plan.parts for f in part.files if f.path == h.file), None)
    diff = own.text if own else "(this file is not part of the change; use the tools to read it)"
    limit = settings.budgets.investigator_diff_chars
    if len(diff) > limit:
        diff = diff[:limit] + "\n… (the rest of this file's change is not shown; read it with the tools)"
    body = (
        f"<description>\n{change.description[:2000] or '(no commit messages yet)'}\n</description>\n\n"
        f"<files_in_change>\n{review_plan.overview[:3000]}\n</files_in_change>\n\n<diff>\n{diff}\n</diff>"
    )
    return [{"type": "text", "text": body, "cache_control": {"type": "ephemeral"}}]


def _system(instructions: str, shared: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [*shared, {"type": "text", "text": instructions}]


def _where(verdict: Verdict, hypothesis: Hypothesis) -> tuple[str, int]:
    if verdict.evidence:
        return verdict.evidence[0].file, verdict.evidence[0].start_line
    return hypothesis.file, hypothesis.line


def _evidence_text(verdict: Verdict) -> str:
    return "\n\n".join(f"{e.file}:{e.start_line}-{e.end_line}\n{e.quote}" for e in verdict.evidence)


async def _edit(
    claude: Claude,
    settings: Settings,
    change: Change,
    intent: IntentGuess,
    held: dict[str, tuple[Hypothesis, Verdict, list[str], dict[str, float], Review | None]],
    dropped: list[Dropped],
) -> list[Finding]:
    """Merge findings that share a root cause, sort them, and write them for the author."""
    blocks = []
    for h, verdict, _, scores, ruling in held.values():
        status = "verified against the code" if ruling else "could not be settled from the code alone"
        blocks.append(
            f"[{h.id}] ({h.bug_class}; {status})\n"
            f"Claim: {ruling.title if ruling else h.statement}\n"
            f"Reasoning: {verdict.reasoning}\n"
            f"Trigger: {verdict.trigger or '(none stated)'}\n"
            + (f"Fast check, author's description mentions this: {scores['described']:.2f} (0 = not at all)\n"
               if "described" in scores else "")
            + f""
            f"Question the investigator would ask: {verdict.question_for_author or '(none)'}\n"
            f"Evidence:\n{_evidence_text(verdict)}"
        )
    claims = "\n".join(f"- {c.text}" for c in intent.claims) or "(none recovered)"
    user = (
        f"<description>\n{change.description or '(no commit messages)'}\n</description>\n\n"
        f"What the author appears to claim:\n{claims}\n\nFindings:\n\n" + "\n\n".join(blocks)
    )
    try:
        result = await claude.structured(
            model=settings.models.edit,
            system=prompts.SORT.format(max_questions=settings.budgets.max_questions,
                                       max_issues=settings.budgets.max_issues),
            user=user,
            output=Sorted,
            effort="medium",
        )
        entries = result.entries
    except CostCapReached:
        raise
    except LLMError:
        entries = []  # fall back to one entry per finding, using the verifier's own ruling

    out: list[Finding] = []
    used: set[str] = set()
    for entry in entries:
        ids = [i for i in entry.ids if i in held and i not in used]
        if not ids:
            continue
        used.update(ids)
        h, verdict, searched, scores, ruling = held[ids[0]]
        file, line = _where(verdict, h)
        out.append(Finding(id=h.id, kind=entry.kind, priority=entry.priority, title=entry.title,
                           detail=entry.detail, file=file,
                           line=line, hypothesis=h, verdict=verdict, review=ruling, question=entry.question,
                           merged=ids[1:], searched=searched, jev=scores))
        for other in ids[1:]:
            dropped.append(Dropped(hypothesis=held[other][0], reason=f"merged into {h.id}", stage="duplicate"))
    for hid, (h, verdict, searched, scores, ruling) in held.items():
        if hid in used:
            continue  # anything the editor skipped is still shown, never silently lost
        file, line = _where(verdict, h)
        kind = ruling.kind if ruling else "decision"
        out.append(Finding(id=h.id, kind=kind, title=ruling.title if ruling else h.statement, file=file,
                           line=line, hypothesis=h, verdict=verdict, review=ruling,
                           question=(ruling.question if ruling else verdict.question_for_author),
                           searched=searched, jev=scores))
    # The caps hold even if the editor ignores them: most important first, the rest kept as background.
    for kind, cap in (("issue", settings.budgets.max_issues), ("decision", settings.budgets.max_questions)):
        of_kind = sorted((f for f in out if f.kind == kind), key=lambda f: f.priority)
        for extra in of_kind[cap:]:
            extra.kind = "background"
    return out


EVIDENCE_CHARS = 6000  # how much quoted evidence the fast model is shown
DESCRIPTION_CHARS = 3000  # how much of the author's description
DIFF_CHARS = 20_000  # how much of a file's diff the contradiction judge reads
MANY_PARTS_CONTEXT_CHARS = 25_000  # context per part when a change is split
SPENT = "not checked: the spending limit was reached"


def _injection_finding(path: str, line: int, text: str) -> Finding:
    ident = f"x{abs(hash(path)) % 1000}-{line}"  # the same line number in two files must not share an id
    hypothesis = Hypothesis(id=ident, statement="The change contains text that instructs an AI reviewer or agent.",
                            bug_class="access_or_security", file=path, line=line, check="read the line")
    verdict = Verdict(status="confirmed", reasoning="A line added in this change is written as an instruction to an AI.",
                      trigger="Any AI reviewer or coding agent that reads this file.", question_for_author="",
                      evidence=[Evidence(file=path, start_line=line, end_line=line, quote=text)])
    return Finding(id=hypothesis.id, kind="issue", priority="P1", file=path, line=line,
                   title="This change contains text written as instructions to an AI reviewer or agent.",
                   detail="Dowel treated it as data and did not follow it. Other tools that read this file may not.",
                   hypothesis=hypothesis, verdict=verdict)


Held = tuple[Hypothesis, Verdict, list[str], dict[str, float], Review | None]
"""An idea that survived: what it claims, the verdict, what was read, the fast model's scores, the second opinion."""


@dataclass
class _Clock:
    """Times each stage of a review and announces it."""

    emit: Emit
    started: float = field(default_factory=time.monotonic)
    stages: list[StageTiming] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._mark = self.started

    def stage(self, name: str, detail: str) -> None:
        now = time.monotonic()
        self.stages.append(StageTiming(name=name, detail=detail, seconds=now - self._mark))
        self.emit(Event("stage", detail, now - self.started))
        self._mark = now

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


@dataclass
class _Run:
    """One review in progress: what every stage needs, and the stages themselves.

    The stages are methods so that each can be read on its own. An idea moves through them by
    `follow`: investigated, gated by the fast model, then tried by a verifier with fresh context.
    """

    repo: Path
    change: Change
    plan: planning.ReviewPlan
    settings: Settings
    emit: Emit
    claude: Claude
    jev: Jev
    shared_parts: list[str]
    dropped: list[Dropped] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.tools = RepoTools(self.repo, self.change.merge_base, self.settings.budgets.tool_result_chars)
        self.gate = asyncio.Semaphore(self.settings.budgets.parallel)
        self._verifying = 0

    @property
    def n_parts(self) -> int:
        return max(len(self.plan.parts), 1)

    @property
    def per_part(self) -> int:
        """How many ideas to ask for from each part of the change."""
        return self.settings.budgets.max_hypotheses if self.n_parts == 1 else max(4, min(8, 60 // self.n_parts))

    @property
    def verifier_budget(self) -> int:
        budgets = self.settings.budgets
        return min(budgets.max_verifier_calls + 2 * (self.n_parts - 1), budgets.max_total_verifier_calls)

    # --- ideas ---------------------------------------------------------------

    async def propose(self, index: int) -> list[Hypothesis]:
        """Ideas about what could be wrong in one part of the change.

        A part with real code in it should yield several. The mid-tier model sometimes stops after
        one, so a thin answer is asked for again, once, with the gap named.
        """
        floor = min(self.per_part, 2 + self.plan.parts[index].source_lines // 40)
        ask = "Propose the hypotheses worth checking for this change."
        best: list[Hypothesis] = []
        async with self.gate:
            for _ in range(2):
                try:
                    found = await self.claude.structured(
                        model=self.settings.models.propose,
                        system=_system(prompts.HYPOTHESES.format(max=self.per_part), self.shared_parts[index]),
                        user=ask,
                        output=HypothesisList,
                        effort="high",  # at medium the mid-tier model stops after one or two ideas
                    )
                except LLMError as exc:  # includes reaching the spending limit: keep what there is
                    if not best:
                        self.emit(Event("note", f"part {index + 1} could not be read: {exc}"))
                    break
                if len(found.hypotheses) > len(best):
                    best = found.hypotheses[: self.per_part]
                if len(best) >= floor:
                    break
                ask = (
                    f"You proposed {len(found.hypotheses)}. A change of this size normally supports at least "
                    f"{floor}. Go through every class in the checklist again, one at a time, against each "
                    "changed function, and give the full list, including what you already proposed."
                )
        return best

    def spread(self, proposed: list[list[Hypothesis]]) -> list[Hypothesis]:
        """Take ideas from every part in turn, so a large change is covered evenly up to the budget."""
        budgets = self.settings.budgets
        budget = min(budgets.max_investigators + 4 * (self.n_parts - 1), budgets.max_total_investigators)
        chosen: list[Hypothesis] = []
        for rank in range(self.per_part):
            for ideas in proposed:
                if rank < len(ideas) and len(chosen) < budget:
                    chosen.append(ideas[rank].model_copy(update={"id": f"h{len(chosen) + 1}"}))
        return chosen

    # --- one idea, start to finish ---------------------------------------------

    async def investigate(self, h: Hypothesis) -> tuple[Verdict | None, list[str], str]:
        """Check one idea against the code with read-only tools. Returns (verdict, what was read, error)."""
        searched: list[str] = []
        async with self.gate:
            try:
                verdict = await self.claude.agent(
                    model=self.settings.models.investigate,
                    system=_system(prompts.INVESTIGATE, _focused_context(self.change, self.plan, h, self.settings)),
                    user=(
                        f"Hypothesis {h.id} ({h.bug_class}) at {h.file}:{h.line}\n"
                        f"{h.statement}\n\nWhat to look at: {h.check}"
                    ),
                    output=Verdict,
                    tools=TOOLS,
                    run_tool=self.tools.run,
                    max_tool_calls=self.settings.budgets.tool_calls_per_investigator,
                    on_tool=lambda n, a: searched.append(describe_call(n, a)),
                )
            except CostCapReached:
                raise
            except Exception as exc:  # noqa: BLE001 - one investigator failing must not end the whole review
                self.emit(Event("check", f"could not check: {h.statement}"))
                return None, searched, str(exc) or type(exc).__name__
        word = {"confirmed": "holds", "refuted": "ruled out", "unresolved": "needs you"}[verdict.status]
        self.emit(Event("check", f"{word}: {h.statement}"))
        return verdict, searched, ""

    async def passes_gate(self, h: Hypothesis, verdict: Verdict) -> tuple[dict[str, float], bool]:
        """Two narrow questions for the fast decision model: do the quoted lines support the claim,
        and did the author's description already mention this behaviour?"""
        scores: dict[str, float] = {}
        if verdict.status == "confirmed":
            p = await self.jev.yes(_evidence_text(verdict)[:EVIDENCE_CHARS],
                                   f"These lines of code show that the following is true: {h.statement}",
                                   name="evidence supports the claim")
            if p is not None:
                scores["evidence_supports"] = round(p, 3)
                if p < self.settings.jev_bars.evidence_supports:
                    self.jev.acted()
                    return scores, False
        described = f"Description of the change written by its author:\n{self.change.description[:DESCRIPTION_CHARS] or '(none)'}"
        p = await self.jev.yes(f"{described}\n\nBehaviour: {h.statement}",
                               "The author's description mentions or clearly implies this behaviour.",
                               name="author described this")
        if p is not None:
            scores["described"] = round(p, 3)
        return scores, True

    async def verify(self, h: Hypothesis, verdict: Verdict) -> Review | None:
        """A second model tries to refute a confirmed idea. It sees the claim and evidence, not the diff."""
        async with self.gate:
            try:
                return await self.claude.agent(
                    model=self.settings.models.verify,
                    system=prompts.VERIFY,
                    user=(
                        f"Claim: {h.statement}\n\nSituation said to trigger it: {verdict.trigger}\n\n"
                        f"Evidence quoted:\n{_evidence_text(verdict)}\n\n"
                        f"The other agent's reasoning: {verdict.reasoning}"
                    ),
                    output=Review,
                    tools=TOOLS,
                    run_tool=self.tools.run,
                    max_tool_calls=self.settings.budgets.tool_calls_per_verifier,
                    effort="medium",
                )
            except CostCapReached:
                raise
            except Exception:  # noqa: BLE001 - an unruled finding is dropped with a reason, not a crash
                return None

    async def follow(self, h: Hypothesis) -> tuple[Held | None, Dropped | None, str]:
        """Take one idea through every stage. Returns (kept, dropped, error): exactly one of the first two."""
        def drop(reason: str, stage: str, error: str = "") -> tuple[None, Dropped, str]:
            return None, Dropped(hypothesis=h, reason=reason, stage=stage), error

        try:
            verdict, searched, error = await self.investigate(h)
            if verdict is None:
                return drop(f"could not be checked: {error}", "investigate", error)
            if verdict.status == "refuted":
                return drop(verdict.reasoning, "investigate")
            if verdict.status == "confirmed" and not verdict.evidence:
                return drop("no evidence was quoted", "investigate")
            scores, keep = await self.passes_gate(h, verdict)
            if not keep:
                return drop(f"the quoted lines do not support the claim (score {scores['evidence_supports']:.2f})",
                            "evidence_gate")
            if verdict.status == "unresolved":
                return (h, verdict, searched, scores, None), None, ""
            if self._verifying >= self.verifier_budget:
                return drop("not verified: the limit on second checks was reached", "verify")
            self._verifying += 1
            ruling = await self.verify(h, verdict)
            if ruling is None:
                return drop("the checker could not rule on it", "verify")
            if not ruling.holds:
                return drop(ruling.refutation_attempt, "verify")
            return (h, verdict, searched, scores, ruling), None, ""
        except CostCapReached:
            # Stopping here must not throw away what the other ideas already established.
            return drop(SPENT, "investigate")

    async def check_all(self, hypotheses: list[Hypothesis]) -> dict[str, Held]:
        """Follow every idea at once, each on its own course, and sort what survived from what did not."""
        outcomes = await asyncio.gather(*(self.follow(h) for h in hypotheses))
        # One check failing is noise. All of them failing the same way is a broken key or a service
        # that is down, and must be said plainly instead of reading as "nothing was found".
        failed = [why for _, _, why in outcomes if why]
        if outcomes and len(failed) == len(outcomes):
            self.emit(Event("note", f"no idea could be checked ({failed[0]}). This review found nothing because it "
                                    "could not look, not because there is nothing to find."))
        held: dict[str, Held] = {}
        for kept, gone, _ in outcomes:
            if kept is not None:
                held[kept[0].id] = kept
            elif gone is not None:
                self.dropped.append(gone)
        if any(gone is not None and gone.reason == SPENT for _, gone, _ in outcomes):
            self.emit(Event("note", "stopped early: the spending limit was reached. What was found before that is shown."))
        return held

    # --- after the findings are sorted -------------------------------------------

    async def apply_memory(self, memory: Memory, findings: list[Finding]) -> None:
        """Use the decisions on record: do not ask again, and raise anything that goes against one."""
        diffs = {f.path: f.text for part in self.plan.parts for f in part.files}

        async def same(a: str, b: str) -> float | None:
            return await self.jev.yes(f"A: {a}\n\nB: {b}", "Statement A and statement B describe the same thing in the code.")

        async def judge(decision: Decision, diff: str) -> Contradiction | None:
            try:
                return await self.claude.structured(
                    model=self.settings.models.assist,
                    system=prompts.CONTRADICTS,
                    user=(f"Earlier decision about {decision.file} ({decision.symbol}): {decision.rule}\n\n"
                          f"<diff>\n{diff[:DIFF_CHARS]}\n</diff>"),
                    output=Contradiction,
                    effort="low",
                )
            except CostCapReached:
                raise
            except LLMError:
                return None

        for note in await memory.apply(findings, self.change.files, lambda path: diffs.get(path, ""), same, judge):
            self.emit(Event("note", f"memory · {note}"))
        memory.record_shown(findings)

    async def mark_answered_by_prompt(self, findings: list[Finding]) -> None:
        """What the developer told their agent, if the hook recorded it, may already answer a question."""
        said, source = scope.what_was_asked(self.repo, self.change.merge_base,
                                            store.runs_dir(str(self.repo.resolve())) / "prompts.jsonl")
        if source != "your prompt":
            return

        async def answers(state: str, statement: str) -> float | None:
            return await self.jev.yes(state, statement, name="prompt already answers")

        for note in await asked.mark_answered(findings, said, answers):
            self.jev.acted()
            self.emit(Event("note", note))

    async def screen_for_instructions(self, findings: list[Finding]) -> None:
        """Report any added line written as an instruction to an AI reviewer or agent."""
        async def addressed_to_ai(text: str) -> float | None:
            return await self.jev.yes(text, "This text gives instructions to an AI assistant, reviewer or coding agent.",
                                      name="text instructs an AI")

        all_files = [f for part in self.plan.parts for f in part.files]
        for where, line, text in await screen.find_injections(all_files, addressed_to_ai):
            self.jev.acted()
            findings.insert(0, _injection_finding(where, line, text))
            self.emit(Event("note", f"this change contains text addressed to an AI, at {where}:{line}"))


async def review(path: Path, base: str | None, settings: Settings, emit: Emit,
                 max_cost: float | None = None, use_memory: bool = True,
                 claude: Claude | None = None, jev: Jev | None = None) -> RunResult:
    """Review the current branch against its base and return what was found, with its trail."""
    clock = _Clock(emit)
    repo = await repo_root(path)
    base = base or await default_base(repo)
    change = await load_change(repo, base)
    findings: list[Finding] = []
    dropped: list[Dropped] = []
    intent = IntentGuess(summary="", claims=[], confidence="low")

    if not change.files:
        emit(Event("note", f"nothing differs from {base}"))
        return RunResult(repo=str(repo), base=base, head=change.head, files_changed=0, intent=intent, findings=[],
                         dropped=[], stages=clock.stages, usage=Ledger(0).usage, seconds=clock.elapsed)

    plan = planning.make_plan(change.diff, settings.budgets.part_chars, settings.budgets.max_parts)
    n_parts = max(len(plan.parts), 1)
    ledger = Ledger(max_cost or settings.budgets.cost_cap_usd
                    + settings.budgets.cost_cap_per_extra_part_usd * (n_parts - 1))
    many = f" · {plan.lines_changed} lines in {n_parts} parts" if n_parts > 1 else ""
    clock.stage("read", f"read {len(change.files)} file{'s' if len(change.files) != 1 else ''} against {base}{many}")
    if plan.skipped:
        emit(Event("note", f"set aside {len(plan.skipped)} files (lock files, generated, assets, or beyond one run)"))

    def result(fast: Jev | None) -> RunResult:
        checks = fast.log.checks if fast is not None else []
        return RunResult(
            repo=str(repo), base=base, head=change.head, files_changed=len(change.files),
            lines_changed=plan.lines_changed, parts=n_parts, skipped=plan.skipped,
            fast_checks=[FastCheck(name=c.name, question=c.question, answer=c.answer, ms=c.ms, acted=c.acted) for c in checks],
            fast_check_summary=fast.log.summary() if fast is not None else {},
            intent=intent, findings=findings, dropped=dropped, stages=clock.stages, usage=ledger.usage,
            seconds=clock.elapsed)

    if not plan.parts:
        emit(Event("note", "nothing in this change needs a code review"))
        return result(None)

    packs = await asyncio.gather(*(context_pack.build(repo, change, only=part.paths, max_symbols=30) for part in plan.parts))
    uses = sum(pack.caller_count for pack in packs)
    clock.stage("map", f"mapped {uses} use{'s' if uses != 1 else ''} of what changed")

    claude = claude or Claude(ledger)
    jev = jev or Jev(settings, ledger.usage)
    memory = Memory(repo) if use_memory else None
    intent_task: asyncio.Task[IntentGuess] | None = None
    try:
        remembered = memory.context(change.files) if memory else ""
        context_each = settings.budgets.context_chars if n_parts == 1 else MANY_PARTS_CONTEXT_CHARS
        shared_parts = [_shared_context(change, plan, part,
                                        (remembered + "\n\n" if remembered else "") + pack.render(context_each), settings)
                        for part, pack in zip(plan.parts, packs, strict=True)]
        run = _Run(repo, change, plan, settings, emit, claude, jev, shared_parts, dropped)

        intent_task = asyncio.create_task(claude.structured(
            model=settings.models.assist, system=_system(prompts.INTENT, shared_parts[0]),
            user="Summarise this change and list what its author claims about it.", output=IntentGuess, effort="low"))

        hypotheses = run.spread(await asyncio.gather(*(run.propose(i) for i in range(n_parts))))
        clock.stage("ideas", f"{len(hypotheses)} idea{'s' if len(hypotheses) != 1 else ''} about what could break")

        held = await run.check_all(hypotheses)
        clock.stage("check", f"checked each one · {len(dropped)} dropped")

        try:
            intent = await intent_task
        except LLMError as exc:
            emit(Event("note", f"could not summarise the change: {exc}"))
        if held:
            findings.extend(await _edit(claude, settings, change, intent, held, dropped))
        if memory is not None:
            await run.apply_memory(memory, findings)
        await run.mark_answered_by_prompt(findings)
        await run.screen_for_instructions(findings)
    except CostCapReached as exc:
        emit(Event("note", f"stopped early: {exc}"))
    finally:
        if intent_task is not None and not intent_task.done():
            intent_task.cancel()
        await jev.aclose()
        if memory is not None:
            memory.close()

    issues = sum(1 for f in findings if f.kind == "issue")
    asks = sum(1 for f in findings if f.kind == "decision")
    clock.stage("done", f"done · {issues} issue{'s' if issues != 1 else ''}, "
                        f"{asks} thing{'s' if asks != 1 else ''} only you can decide")
    for f in findings:
        emit(Event("finding", f.title, finding=f))
    return result(jev)
