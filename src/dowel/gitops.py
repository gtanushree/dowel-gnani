"""Git helpers. Dowel reads history and diffs; the only files it writes to a repository are its own, under .dowel/."""

from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


async def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise GitError(err.decode(errors="replace").strip() or f"git {' '.join(args)} failed")
    return out.decode(errors="replace")


GIT_TIMEOUT_S = 20


def run_git(repo: Path, *args: str, check: bool = False, timeout: float = GIT_TIMEOUT_S) -> str:
    """Run git and return what it printed. For code that is not async.

    With `check` off, a failing or slow git gives an empty string: callers that only want to know
    "is there anything here" should not each have to handle errors.
    """
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if check:
            raise GitError(f"git {' '.join(args[:2])} could not run: {exc}") from exc
        return ""
    if check and done.returncode != 0:
        raise GitError(done.stderr.strip() or f"git {' '.join(args)} failed")
    return done.stdout


def renames(repo: Path, limit: int = 400) -> dict[str, str]:
    """Old path -> newest path, from git's own rename tracking over the last `limit` commits."""
    out = run_git(repo, "log", "-M", "--diff-filter=R", "--name-status", "--format=", f"-n{limit}", timeout=10)
    step: dict[str, str] = {}
    for line in reversed(out.splitlines()):  # oldest first, so a later rename wins
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            step[parts[1]] = parts[2]
    final = {}
    for old, first in step.items():
        new, seen = first, {old}
        while new in step and new not in seen:
            seen.add(new)
            new = step[new]
        final[old] = new
    return final


async def repo_root(path: Path) -> Path:
    try:
        return Path((await git(path, "rev-parse", "--show-toplevel")).strip())
    except GitError as exc:
        raise GitError(f"{path} is not inside a git repository") from exc


async def default_base(repo: Path) -> str:
    """The branch this work most likely started from."""
    head = (await git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)).strip()
    if head:
        return head.removeprefix("refs/remotes/")
    for name in ("main", "master", "dev", "develop", "origin/main", "origin/master"):
        if (await git(repo, "rev-parse", "--verify", "--quiet", name, check=False)).strip():
            return name
    raise GitError("could not tell which branch to compare against; pass --base")


@dataclass(frozen=True)
class Change:
    base: str
    head: str
    merge_base: str
    files: list[str]
    diff: str
    description: str  # commit messages on the branch, newest last


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


async def load_change(repo: Path, base: str) -> Change:
    head = (await git(repo, "rev-parse", "--abbrev-ref", "HEAD")).strip()
    merge_base = (await git(repo, "merge-base", base, "HEAD")).strip()
    # Compare the merge base with the working tree so uncommitted edits are reviewed too.
    files = [f for f in (await git(repo, "diff", "--name-only", merge_base)).splitlines() if f]
    diff = await git(repo, "diff", "--unified=6", "--no-color", merge_base)
    log = await git(repo, "log", "--reverse", "--format=%s%n%b", f"{merge_base}..HEAD")
    return Change(base=base, head=head, merge_base=merge_base, files=files, diff=diff,
                  description=log.strip())


def changed_lines(diff: str) -> dict[str, list[tuple[int, int]]]:
    """New-file line ranges touched, per path."""
    out: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            current = None if path == "/dev/null" else path.removeprefix("b/")
        elif current and (m := _HUNK.match(line)):
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out.setdefault(current, []).append((start, start + max(count, 1) - 1))
    return out
