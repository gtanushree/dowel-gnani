"""The brief a reviewer reads before the diff.

Four short parts, in the order a reviewer needs them: what nobody asked for, what the author
decided and why, what happened to the tests, and what is still open. Then where to start reading.

It is assembled from git, the decision files and the last saved review. No model writes it, so it
cannot pad, and the author's words appear as they gave them.
"""

from __future__ import annotations

from pathlib import Path

from dowel import check, scope, store
from dowel import decisions as files
from dowel.gitops import run_git


def _new_decisions(repo: Path, merge_base: str) -> list[files.Record]:
    """Decision records this change adds, committed or not."""
    added = {row.split("\t")[-1] for row in run_git(repo, "diff", "--name-status", "--diff-filter=A", merge_base, "--",
                                                 files.FOLDER).splitlines() if row}
    added |= set(run_git(repo, "ls-files", "--others", "--exclude-standard", "--", files.FOLDER).splitlines())
    return [r for r in files.read_all(repo) if r.file in added]


def _quote(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build(repo: Path, merge_base: str, base_name: str = "") -> str:
    branch = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    prompts = store.runs_dir(str(repo.resolve())) / "prompts.jsonl"
    sc = scope.build(repo, merge_base, prompts)
    report = check.gather(repo, merge_base)
    records = _new_decisions(repo, merge_base)
    out = [f"# Brief for the reviewer: {branch}" + (f" against {base_name}" if base_name else ""), ""]

    if sc.asked:
        out += [f"**What was asked** (from {sc.source}): {_quote(sc.asked)}", ""]
    else:
        out += ["**What was asked:** nothing recorded says. The labels below cannot tell asked-for from not.", ""]

    unexplained = sc.of(scope.UNEXPLAINED)
    out.append(f"## Not explained ({len(unexplained)})")
    out += [f"- `{e.path}` (+{e.added} −{e.removed}): {e.why}" for e in unexplained[:12]] or ["- Nothing."]
    if len(unexplained) > 12:
        out.append(f"- and {len(unexplained) - 12} more")
    out.append("")

    made = [r for r in records if r.kind == "decision" and r.bug_class != check.ACCEPTED]
    retired = [r for r in records if r.kind == "retire"]
    out.append(f"## Decided by the author ({len(made) + len(retired)})")
    for r in made:
        verdict = {"keep": "Keeping it.", "change": "This will change.", "unsure": "Not sure; needs a second opinion."}.get(r.stance, "")
        where = f" `{r.path}`" + (f" · `{r.symbol}`" if r.symbol else "")
        said = f' "{_quote(r.reason)}"' if r.reason else " No reason given."
        how = f" ({r.given})" if r.given else ""
        out.append(f"- {_quote(r.title)} **{verdict}**{said}{how}{where}")
    out += [f"- Retired an earlier decision: {_quote(r.reason) or 'no reason given'}" for r in retired]
    if not made and not retired:
        out.append("- No decisions were recorded with this change.")
    out.append("")

    accepted = [r for r in records if r.bug_class == check.ACCEPTED]
    weak = [p for p in report.problems if p.kind == "test"]
    if accepted or weak:
        out.append("## Tests made weaker")
        out += [f"- `{p.where}`: {p.what}. **No reason recorded.**" for p in weak]
        out += [f'- {_quote(r.title)} Author\'s reason: "{_quote(r.reason)}"' for r in accepted]
        out.append("")

    still = [f"- `{p.where}`: {p.what}" for p in report.problems if p.kind != "test"]
    run = store.load_run(str(repo.resolve()))
    if run is not None and run.head == branch:
        state = store.load_state(str(repo.resolve()), run)
        for f in run.findings:
            item = state.items.get(f.id)
            if f.kind == "background" or item is None or not item.included or item.noise:
                continue
            if f.kind == "decision" and item.answer is None:
                still.append(f"- Not answered yet: {_quote(f.title)} (`{f.file}:{f.line}`)")
            elif f.kind == "issue" and item.answer != "keep":
                still.append(f"- Found by the review, not yet fixed: {_quote(f.title)} (`{f.file}:{f.line}`)")
    out.append(f"## Still open ({len(still)})")
    out += still[:10] or ["- Nothing."]
    out.append("")

    out.append("## Where to start")
    reading = [e for e in sc.entries if e.label != scope.MECHANICAL]
    out += [f"{n}. `{e.path}` (+{e.added} −{e.removed}), {e.label}" for n, e in enumerate(reading[:8], 1)]
    folded = len(sc.of(scope.MECHANICAL)) + max(0, len(reading) - 8)
    if folded:
        out.append(f"\n{folded} other file{'s' if folded != 1 else ''} left out here: mechanical changes or lower in the order.")
    return "\n".join(out).rstrip() + "\n"
