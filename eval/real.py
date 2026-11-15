"""Run the engine on real merged changes of different sizes and report time, cost and findings.

    uv run python eval/real.py <clones-dir> [--dry] [case ...]

Each case checks out the merged branch in a throwaway clone and reviews it against the branch it
was merged into. --dry only shows how each change would be split, with no model calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from dowel.config import Settings
from dowel.gitops import load_change
from dowel.pipeline import review
from dowel.plan import make_plan

# name -> (clone folder, merge commit). Kept in an untracked file because it names private repositories:
#   eval/cases.local.json  {"small": ["myrepo", "abc1234"], ...}
_LOCAL = Path(__file__).parent / "cases.local.json"
CASES: dict[str, tuple[str, str]] = {k: tuple(v) for k, v in json.loads(_LOCAL.read_text()).items()} if _LOCAL.exists() else {}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    root, names = Path(args[0]), (args[1:] or list(CASES))
    settings = Settings()
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    for name in names:
        repo_name, merge = CASES[name]
        repo = root / repo_name
        base, head = git(repo, "rev-parse", f"{merge}^1"), git(repo, "rev-parse", f"{merge}^2")
        git(repo, "checkout", "-q", "--detach", head)
        change = await load_change(repo, base)
        plan = make_plan(change.diff, settings.budgets.part_chars, settings.budgets.max_parts)
        reviewed = sum(len(p.files) for p in plan.parts)
        print(f"\n== {name}: {plan.lines_changed} lines, {len(change.files)} files, diff {len(change.diff) // 1000}k chars "
              f"-> {len(plan.parts)} parts, {reviewed} files reviewed, {len(plan.skipped)} set aside")
        if dry:
            reasons: dict[str, int] = {}
            for line in plan.skipped:
                reasons[line.rsplit(": ", 1)[1]] = reasons.get(line.rsplit(": ", 1)[1], 0) + 1
            print("   set aside:", reasons or "nothing")
            continue
        limit = float(os.environ["DOWEL_MAX_COST"]) if os.environ.get("DOWEL_MAX_COST") else None
        result = await review(repo, base, settings, lambda e: print(f"   {e.seconds:5.0f}s  {e.text}") if e.kind != "finding" else None, limit, use_memory=False)
        kinds = [f.kind for f in result.findings]
        print(f"   RESULT {result.seconds:.0f}s  ${result.usage.cost_usd:.2f}  {kinds.count('issue')} issues, "
              f"{kinds.count('decision')} questions, {kinds.count('background')} background, {len(result.dropped)} dropped, "
              f"jev calls {result.usage.jev_calls}")
        for f in result.findings:
            if f.kind != "background":
                print(f"     [{f.kind:<8}] {f.file}:{f.line}  {f.title} {f.question}".rstrip())
        (out_dir / f"{name}.json").write_text(result.model_dump_json(indent=2))
        print(json.dumps({"case": name, "lines": result.lines_changed, "parts": result.parts, "seconds": round(result.seconds),
                          "cost": round(result.usage.cost_usd, 2), "stages": {s.name: round(s.seconds) for s in result.stages}}))


if __name__ == "__main__":
    asyncio.run(main())
