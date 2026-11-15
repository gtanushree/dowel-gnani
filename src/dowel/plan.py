"""Splitting a large change into parts the engine can read, and saying what it leaves out.

A model reads a bounded amount well. A 30,000-line change is split by file into parts of a fixed
size, each reviewed with its own context, and files that are not worth a reviewer's attention
(lock files, generated code, images) are set aside by name. Nothing is dropped silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_SKIP_NAMES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock", "Cargo.lock",
               "go.sum", "composer.lock", "Gemfile.lock", "bun.lockb"}
_SKIP_SUFFIXES = (".lock", ".min.js", ".min.css", ".map", ".snap", ".svg", ".png", ".jpg", ".jpeg", ".gif",
                  ".webp", ".ico", ".pdf", ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".wav", ".zip", ".csv",
                  ".ipynb")
_SKIP_DIRS = {"node_modules", "dist", "build", "vendor", ".next", "__snapshots__", "__generated__", "generated"}
_GENERATED = re.compile(r"(_pb2\.py|\.pb\.go|\.gen\.[a-z]+|\.generated\.[a-z]+)$")
_DOCS = (".md", ".mdx", ".rst", ".txt")
_TEST = re.compile(r"(^|/)(tests?|__tests__|spec|e2e|fixtures)(/|$)|(_test\.|\.test\.|\.spec\.|^test_|/test_)")


@dataclass(frozen=True)
class FileDiff:
    path: str
    text: str
    added: int
    removed: int

    @property
    def is_test(self) -> bool:
        return bool(_TEST.search(self.path))


@dataclass
class Part:
    files: list[FileDiff] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(f.text) for f in self.files)

    @property
    def paths(self) -> set[str]:
        return {f.path for f in self.files}

    @property
    def source_lines(self) -> int:
        return sum(f.added + f.removed for f in self.files if not f.is_test)

    def diff(self, limit: int) -> str:
        text = "\n".join(f.text for f in self.files)
        return text if len(text) <= limit else text[:limit] + "\n… (this file's diff is longer than one part; the rest was not read)"


@dataclass
class ReviewPlan:
    parts: list[Part]
    skipped: list[str]  # "path: reason"
    lines_changed: int
    overview: str


def split_diff(diff: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    for block in re.split(r"(?m)^(?=diff --git )", diff):
        if not block.startswith("diff --git "):
            continue
        match = re.search(r"(?m)^\+\+\+ b/(.+)$", block) or re.search(r"(?m)^--- a/(.+)$", block)
        if match is None:
            header = re.match(r"diff --git a/(.+?) b/", block)
            if header is None:
                continue
            path = header.group(1)
        else:
            path = match.group(1).strip()
        added = len(re.findall(r"(?m)^\+(?!\+\+ )", block))
        removed = len(re.findall(r"(?m)^-(?!-- )", block))
        files.append(FileDiff(path=path, text=block, added=added, removed=removed))
    return files


def skip_reason(file: FileDiff) -> str | None:
    path = PurePosixPath(file.path)
    if "Binary files" in file.text[:400] or "GIT binary patch" in file.text[:400]:
        return "binary file"
    if path.name in _SKIP_NAMES or file.path.endswith(_SKIP_SUFFIXES):
        return "lock file, asset or data"
    if _SKIP_DIRS.intersection(path.parts) or _GENERATED.search(file.path):
        return "generated or vendored"
    if file.added == 0:
        return "only deletions"
    return None


def make_plan(diff: str, part_chars: int, max_parts: int) -> ReviewPlan:
    files = split_diff(diff)
    skipped: list[str] = []
    keep: list[FileDiff] = []
    for f in files:
        reason = skip_reason(f)
        if reason:
            skipped.append(f"{f.path}: {reason}")
        else:
            keep.append(f)

    # Neighbouring files usually belong together, so fill parts in path order.
    parts: list[Part] = []
    current = Part()
    for f in sorted(keep, key=lambda f: f.path):
        if current.files and current.chars + len(f.text) > part_chars:
            parts.append(current)
            current = Part()
        current.files.append(f)
    if current.files:
        parts.append(current)

    if len(parts) > max_parts:
        # Too much for one run: keep the parts with the most non-test code, and say what was left.
        ranked = sorted(parts, key=lambda p: p.source_lines, reverse=True)
        for part in ranked[max_parts:]:
            skipped.extend(f"{f.path}: beyond what one run reads; review it on its own" for f in part.files)
        parts = [p for p in parts if p in ranked[:max_parts]]

    lines = [f"{f.path}  +{f.added} -{f.removed}" for f in sorted(keep, key=lambda f: f.path)]
    overview = "\n".join(lines)
    if len(overview) > 8000:
        overview = overview[:8000] + f"\n… and more ({len(keep)} files in all)"
    return ReviewPlan(parts=parts, skipped=skipped, lines_changed=sum(f.added + f.removed for f in files),
                      overview=overview)
