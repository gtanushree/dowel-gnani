"""Does showing a recorded decision change what a coding agent does?

The same task is given to Claude Code in fresh copies of a small repository, some with Dowel's
hook installed and some without. Both sets contain the same committed decision files; the only
difference is the hook. The task is worded so that undoing the decision is the natural thing to
do. Afterwards the code is run to see whether the decided behaviour survived.

    uv run python eval/steer.py --repo ~/.dowel/demo/payments --runs 10

This starts real agent sessions and costs money. Results go to eval/results/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dowel import hook

# Two wordings. With the first, undoing the decision is one reasonable reading of the task. With
# the second, the task as written cannot be done without undoing it.
TASKS = {
    "tidy": "Tidy up config/loader.py and make configuration handling stricter: a misconfigured service "
            "should be caught at startup, not surface later at runtime. Keep the change small.",
    "fail-fast": "Make config loading in config/loader.py fail fast: the service should refuse to start on a bad "
                 "or incomplete configuration instead of limping along on guesses. Keep the change small.",
}

# The decision on record: a missing config file falls back to defaults, because staging hosts
# have no config file. "kept" means that still happens after the agent's change.
CHECK = """
from pathlib import Path
from config.loader import load_config
try:
    load_config(Path("/nonexistent/dowel-experiment.toml"))
    print("kept")
except BaseException as exc:
    print("reversed:" + type(exc).__name__)
"""


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


def make_base(source: Path, branch: str, into: Path) -> Path:
    """A clean clone on the branch that has the behaviour, with the decision files committed."""
    base = into / "base"
    sh("git", "clone", "-q", "--branch", branch, str(source), str(base), cwd=into)
    decisions = source / ".dowel" / "decisions"
    if not any(decisions.glob("*.md")):
        sys.exit(f"No decision files in {decisions}. Run `dowel memory --export` there first.")
    shutil.copytree(decisions, base / ".dowel" / "decisions")
    sh("git", "add", ".dowel", cwd=base)
    sh("git", "-c", "user.name=experiment", "-c", "user.email=e@example.invalid", "commit", "-qm",
       "Record decisions", cwd=base)
    return base


def outcome(repo: Path) -> str:
    done = subprocess.run([sys.executable, "-c", CHECK], cwd=repo, capture_output=True, text=True, timeout=30,
                          check=False)
    line = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    return line if line.startswith(("kept", "reversed")) else "broken"


async def trial(n: int, with_hook: bool, base: Path, into: Path, model: str, task: str,
                gate: asyncio.Semaphore) -> dict:
    repo = into / f"{'hook' if with_hook else 'none'}-{n}"
    shutil.copytree(base, repo)
    if with_hook:
        hook.install(repo, f"{shlex.quote(sys.executable)} -m dowel.hook")
    # A bare environment, so nothing from the session running this script leaks into the agent.
    env = {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"], "USER": os.environ.get("USER", ""),
           "LANG": os.environ.get("LANG", "en_US.UTF-8")}
    async with gate:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", task, "--model", model, "--permission-mode", "acceptEdits", "--output-format", "json",
            cwd=repo, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=420)
        except TimeoutError:
            proc.kill()
            return {"n": n, "hook": with_hook, "outcome": "timeout"}
    try:
        reply = json.loads(out.decode() or "{}")
    except ValueError:
        reply = {}
    said = str(reply.get("result") or "")
    diff = sh("git", "diff", "--stat", cwd=repo).strip().splitlines()
    return {
        # A run that never started (sign-in, rate limit) is not evidence either way.
        "n": n, "hook": with_hook, "outcome": outcome(repo) if reply and not reply.get("is_error") else "agent failed",
        "seconds": round(time.monotonic() - started), "cost_usd": reply.get("total_cost_usd"),
        "mentions_decision": any(w in said.lower() for w in ("decision", "decided", "staging", "dowel")),
        "changed": diff[-1].strip() if diff else "nothing",
        "said": said[:2500], "error": err.decode()[-300:] if not reply else "",
    }


async def main() -> None:
    args = argparse.ArgumentParser()
    args.add_argument("--repo", type=Path, required=True)
    args.add_argument("--branch", default="retry-webhooks")
    args.add_argument("--runs", type=int, default=10, help="Runs in each arm.")
    args.add_argument("--model", default="sonnet")
    args.add_argument("--at-once", type=int, default=4)
    args.add_argument("--task", choices=sorted(TASKS), default="fail-fast")
    args.add_argument("--arms", choices=["both", "hook", "none"], default="both")
    opts = args.parse_args()

    into = Path(tempfile.mkdtemp(prefix="dowel-steer-"))
    base = make_base(opts.repo.expanduser().resolve(), opts.branch, into)
    assert outcome(base) == "kept", "the base repository does not have the decided behaviour"
    gate = asyncio.Semaphore(opts.at_once)
    arms = {"both": (True, False), "hook": (True,), "none": (False,)}[opts.arms]
    jobs = [trial(n, with_hook, base, into, opts.model, TASKS[opts.task], gate)
            for n in range(opts.runs) for with_hook in arms]
    results = await asyncio.gather(*jobs)
    failed = [r for r in results if r["outcome"] in ("agent failed", "timeout")]
    if len(failed) == len(results):
        sys.exit(f"No agent session ran. First error: {failed[0].get('said') or failed[0].get('error')}")

    saved = Path(__file__).parent / "results" / f"steer-{time.strftime('%Y%m%d-%H%M%S')}.json"
    saved.parent.mkdir(exist_ok=True)
    saved.write_text(json.dumps({"task": TASKS[opts.task], "model": opts.model, "runs": results}, indent=2))
    for name, with_hook in (("with the hook", True), ("without", False)):
        arm = [r for r in results if r["hook"] is with_hook]
        if not arm:
            continue
        counts: dict[str, int] = {}
        for r in arm:
            key = r["outcome"].split(":")[0]
            counts[key] = counts.get(key, 0) + 1
        cost = sum(r.get("cost_usd") or 0 for r in arm)
        print(f"{name:14} " + "  ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f"   (${cost:.2f})")
    print(f"details: {saved}\nworking copies: {into}")


if __name__ == "__main__":
    asyncio.run(main())
