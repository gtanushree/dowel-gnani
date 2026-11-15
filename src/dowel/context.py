"""The context pack: what surrounds a change. Deterministic, no model calls.

For each changed file it finds the symbols the change defines or touches, then where else in the
repository those symbols are used. It also picks up project rule files from the base branch, so
a change cannot rewrite the rules it is reviewed under.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from dowel import symbols
from dowel.gitops import Change, changed_lines, git

_RULE_FILES = ("REVIEW.md", "AGENTS.md", "CLAUDE.md")
_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:def|class|func|function|fn|interface|type|struct|const|let|var)\s+"
    r"(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)"
)
_NOISE = {"main", "init", "__init__", "test", "setup", "run", "get", "set", "new", "self", "err", "ctx"}


@dataclass
class ContextPack:
    symbols: dict[str, list[str]] = field(default_factory=dict)  # file -> symbols touched
    callers: dict[str, list[str]] = field(default_factory=dict)  # symbol -> "path:line: text"
    rules: str = ""
    caller_count: int = 0

    def render(self, limit: int) -> str:
        parts: list[str] = []
        if self.rules:
            parts.append("## Project rules (from the base branch)\n" + self.rules)
        if self.symbols:
            lines = [f"- {path}: {', '.join(names)}" for path, names in self.symbols.items()]
            parts.append("## Symbols this change touches\n" + "\n".join(lines))
        if self.callers:
            blocks = []
            for symbol, uses in self.callers.items():
                blocks.append(f"### {symbol} is used at\n" + "\n".join(uses))
            parts.append("## Where those symbols are used elsewhere\n" + "\n\n".join(blocks))
        text = "\n\n".join(parts)
        return text if len(text) <= limit else text[:limit] + "\n… (context trimmed to fit)"


def _generic_symbols(source: str, ranges: list[tuple[int, int]]) -> list[str]:
    """Nearest definition at or above each changed range, plus definitions inside it."""
    lines = source.splitlines()
    found: list[str] = []
    for lo, hi in ranges:
        for i in range(min(hi, len(lines)), 0, -1):
            m = _DEF.match(lines[i - 1])
            if m:
                found.append(m.group(1))
                if i < lo:
                    break
    return found


async def build(repo: Path, change: Change, limit_uses: int = 12, only: set[str] | None = None,
                max_symbols: int = 40) -> ContextPack:
    pack = ContextPack()
    ranges = changed_lines(change.diff)
    if only is not None:
        ranges = {path: spans for path, spans in ranges.items() if path in only}

    for path, spans in ranges.items():
        file = repo / path
        if not file.is_file():
            continue
        source = file.read_text(errors="replace")
        names = symbols.touched(path, source, spans)
        if names is None:  # a language without a parser: fall back to pattern matching
            names = _generic_symbols(source, spans)
        names = [n for n in dict.fromkeys(names) if n.lower() not in _NOISE and len(n) > 2]
        if names:
            pack.symbols[path] = names[:12]

    seen: set[str] = set()
    for path, names in pack.symbols.items():
        for name in names:
            if name in seen or len(seen) >= max_symbols:
                continue
            seen.add(name)
            out = await git(repo, "grep", "-n", "-I", "-w", "-F", "-e", name, "--", ".", ":!.env*", check=False)
            uses = [u for u in out.splitlines() if u and not u.startswith(path + ":")]
            if uses:
                pack.caller_count += len(uses)
                extra = f"\n… and {len(uses) - limit_uses} more" if len(uses) > limit_uses else ""
                pack.callers[name] = [u[:220] for u in uses[:limit_uses]] + ([extra.strip()] if extra else [])

    rules: list[str] = []
    for name in _RULE_FILES:
        text = await git(repo, "show", f"{change.merge_base}:{name}", check=False)
        if text.strip():
            rules.append(f"### {name}\n{text.strip()[:4000]}")
    pack.rules = "\n\n".join(rules)
    return pack
