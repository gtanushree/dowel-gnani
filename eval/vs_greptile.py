"""Compare Dowel with Greptile on real pull requests, at the commit Greptile actually reviewed.

    uv run python eval/vs_greptile.py <clones-dir> <repo> <pr> [<pr> ...]

Reads eval/greptile/prs.json (collected with the GitHub CLI). A Greptile comment counts as matched
when Dowel raised something in the same file within 25 lines; the rest are printed for reading.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from dowel.config import Settings
from dowel.pipeline import review

HERE = Path(__file__).parent


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True).stdout.strip()


def title(body: str) -> str:
    name = re.search(r"\*\*(.+?)\*\*", body)
    level = re.search(r'alt="(P\d)"', body)
    return f"{level.group(1) if level else '--'} {name.group(1) if name else body[:70]}"


async def main() -> None:
    root, repo_name, numbers = Path(sys.argv[1]), sys.argv[2], [int(n) for n in sys.argv[3:]]
    rows = {(r["repo"], r["number"]): r for r in json.loads((HERE / "greptile" / "prs.json").read_text())}
    repo = root / repo_name
    totals = Counter()
    for number in numbers:
        pr = rows[(repo_name, number)]
        commit = Counter(c["commit"] for c in pr["inline"]).most_common(1)[0][0]
        comments = [c for c in pr["inline"] if c["commit"] == commit]
        if not git(repo, "cat-file", "-t", commit, check=False):
            git(repo, "fetch", "-q", f"https://github.com/{os.environ['DOWEL_EVAL_ORG']}/{repo_name}.git", f"pull/{number}/head", check=False)
        git(repo, "checkout", "-q", "--detach", commit)
        print(f"\n== {repo_name} #{number} at {commit[:8]}: {len(comments)} Greptile comments")
        result = await review(repo, pr["base"], Settings(), lambda _e: None, use_memory=False)
        shown = [f for f in result.findings if f.kind != "background"]
        print(f"   Dowel: {result.lines_changed} lines, {result.parts} parts, {result.seconds:.0f}s, ${result.usage.cost_usd:.2f}, "
              f"{len(shown)} shown, {len(result.dropped)} dropped")
        used: set[str] = set()
        for c in comments:
            same_file = [f for f in shown if f.file == c["path"]]
            close = [f for f in same_file if c["line"] and abs(f.line - c["line"]) <= 25]
            dropped = [d for d in result.dropped if d.hypothesis.file == c["path"]]
            verdict = "same spot" if close else "same file" if same_file else "dropped" if dropped else "missed"
            totals[verdict] += 1
            print(f"   G {verdict:<10} {c['path']}:{c['line']}  {title(c['body'])}")
            for f in close or same_file[:2]:
                used.add(f.id)
                print(f"       Q [{f.kind}] :{f.line} {f.title}")
            if verdict == "dropped":
                print(f"       Q dropped ({dropped[0].stage}): {dropped[0].hypothesis.statement[:120]}")
        for f in shown:
            if f.id not in used:
                print(f"   Q only [{f.kind}] {f.file}:{f.line} {f.title}")
        totals["dowel_only"] += sum(f.id not in used for f in shown)
        (HERE / "results" / f"{repo_name}-{number}.json").write_text(result.model_dump_json(indent=2))
    print("\nTOTAL", dict(totals))


if __name__ == "__main__":
    asyncio.run(main())
