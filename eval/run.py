"""Score the engine on a seeded repository.

    uv run python eval/run.py payments --runs 3

For each planted problem it reports how often it was found and how often it was sorted as
expected. Each run makes real model calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import tempfile
from pathlib import Path

from dowel.config import Settings
from dowel.pipeline import review
from dowel.schemas import Finding, RunResult

HERE = Path(__file__).parent


def _text(f: Finding) -> str:
    return " ".join([f.title, f.detail, f.question, f.hypothesis.statement])


def score(result: RunResult, expected: list[dict]) -> dict[str, tuple[bool, bool]]:
    shown = [f for f in result.findings if f.kind != "background"]
    out: dict[str, tuple[bool, bool]] = {}
    for item in expected:
        hits = [f for f in shown if re.search(item["match"], _text(f), re.IGNORECASE)]
        kind_ok = any(item["kind"] in ("any", f.kind) for f in hits)
        out[item["id"]] = (bool(hits), kind_ok)
    return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("seed")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    spec = json.loads((HERE / "seeds" / f"{args.seed}.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / args.seed
        subprocess.run([str(HERE / "seeds" / f"{args.seed}.sh"), str(repo)], check=True)
        results: list[RunResult] = []
        for i in range(args.runs):
            result = await review(repo, spec["base"], Settings(), lambda _event: None, use_memory=False)
            results.append(result)
            kinds = [f.kind for f in result.findings]
            print(f"run {i + 1}: {result.seconds:.0f}s  ${result.usage.cost_usd:.2f}  "
                  f"{kinds.count('issue')} issues, {kinds.count('decision')} decisions, "
                  f"{kinds.count('background')} background, {len(result.dropped)} dropped")
            for f in result.findings:
                print(f"    [{f.kind:<10}] {f.title} {f.question}".rstrip())

    print(f"\n{'planted problem':<28}{'found':>8}{'sorted right':>15}")
    scores = [score(r, spec["expected"]) for r in results]
    for item in spec["expected"]:
        found = sum(s[item["id"]][0] for s in scores)
        right = sum(s[item["id"]][1] for s in scores)
        print(f"{item['id']:<28}{f'{found}/{len(scores)}':>8}{f'{right}/{len(scores)}':>15}")
    shown = [sum(f.kind != "background" for f in r.findings) for r in results]
    print(f"\nitems shown per run: {shown}   mean time {sum(r.seconds for r in results) / len(results):.0f}s   "
          f"mean cost ${sum(r.usage.cost_usd for r in results) / len(results):.2f}")


if __name__ == "__main__":
    asyncio.run(main())
