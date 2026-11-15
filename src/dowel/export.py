"""Plain-text exports from a run: the summary for a reviewer, the prompt for a coding agent,
and the why note. Only what the author left ticked is included, and nothing judges the author."""

from __future__ import annotations

from dowel.schemas import Finding, RunResult
from dowel.store import ItemState, RunState


def _at(f: Finding) -> str:
    return f"{f.file}:{f.line}"


def _shared(run: RunResult, state: RunState) -> list[tuple[Finding, ItemState]]:
    out = []
    for f in run.findings:
        item = state.items.get(f.id, ItemState())
        if f.kind != "background" and item.included and not item.noise:
            out.append((f, item))
    return out


def label(f: Finding, item: ItemState) -> str:
    if f.memory and item.answer is None:
        return "from memory"
    if f.kind == "issue":
        return "dowel found"
    if item.answer in ("keep", "change"):
        return "you decided"
    if item.answer == "unsure":
        return "worth a second look"
    return "for you"


def _decided_line(f: Finding, item: ItemState) -> str:
    verdict = "Keeping this." if item.answer == "keep" else "This will change."
    note = f' "{item.note.strip()}"' if item.note.strip() else ""
    return f"- {f.title} {verdict}{note} ({_at(f)})"


def summary(run: RunResult, state: RunState) -> str:
    shared = _shared(run, state)
    decided = [(f, i) for f, i in shared if f.kind == "decision" and i.answer in ("keep", "change")]
    look = [(f, i) for f, i in shared if f.kind == "decision" and i.answer not in ("keep", "change")]
    found = [(f, i) for f, i in shared if f.kind == "issue"]

    lines = [f"Dowel review of {run.head} against {run.base}", ""]
    if run.intent.summary:
        lines += [run.intent.summary, ""]
    focus = (look or found or decided)
    if focus:
        f = focus[0][0]
        lines += [f"Spend your review time on {_at(f)}: {f.title}", ""]
    if decided:
        lines += ["Decided by the author", *[_decided_line(f, i) for f, i in decided], ""]
    if look:
        lines.append("Worth a second look")
        for f, i in look:
            note = f' Author\'s note: "{i.note.strip()}"' if i.note.strip() else ""
            lines.append(f"- {f.title}{note} ({_at(f)})")
        lines.append("")
    if found:
        lines += ["Found by Dowel", *[f"- {f.title} ({_at(f)})" for f, _ in found], ""]
    if not shared:
        lines += ["Nothing was shared from this review.", ""]
    lines.append("The author chose what to share. This points to where to look; it does not replace reading the change.")
    return "\n".join(lines)


def agent_prompt(run: RunResult, state: RunState) -> str:
    """Instructions a coding agent can act on: confirmed issues, and decisions the author wants changed."""
    todo: list[str] = []
    for f, item in _shared(run, state):
        if f.kind == "issue":
            evidence = f.verdict.evidence[0].quote.strip().splitlines()[0] if f.verdict.evidence else ""
            line = f"{f.title} ({_at(f)})"
            if f.detail:
                line += f" {f.detail}"
            if evidence:
                line += f"\n   Evidence: `{evidence}`"
            todo.append(line)
        elif item.answer == "change":
            line = f"{f.title} ({_at(f)}) I do not want this behaviour."
            if item.note.strip():
                line += f" What I want instead: {item.note.strip()}"
            todo.append(line)
    if not todo:
        return "Nothing to fix from this review."
    body = "\n".join(f"{n}. {line}" for n, line in enumerate(todo, 1))
    return (
        f"On branch {run.head}, make these changes. Each was checked against the code. Keep every other "
        f"behaviour as it is, and add or update a test for each fix.\n\n{body}"
    )


def why_note(run: RunResult, state: RunState) -> dict[str, list[str]]:
    decided, still_open = [], []
    for f, item in _shared(run, state):
        if f.kind != "decision":
            continue
        if item.answer in ("keep", "change"):
            verdict = "kept" if item.answer == "keep" else "to be changed"
            reason = f": {item.note.strip()}" if item.note.strip() else ""
            decided.append(f"{f.title} ({verdict}{reason})")
        else:
            still_open.append(f.title)
    return {"decided": decided, "open": still_open}
