"""Read-only tools an investigator may use. No writes, no network, no paths outside the repo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dowel.gitops import git

_BLOCKED = {".env", ".git"}


def _tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS: list[dict[str, Any]] = [
    _tool(
        "read_file",
        "Read lines from a file in the repository (the version being reviewed). Returns numbered lines.",
        {
            "path": {"type": "string", "description": "Repo-relative path."},
            "start_line": {"type": "integer", "description": "First line, 1-based."},
            "end_line": {"type": "integer", "description": "Last line, inclusive. At most 200 lines per call."},
        },
        ["path", "start_line", "end_line"],
    ),
    _tool(
        "grep",
        "Search the repository's tracked files for a regular expression. Returns path:line:text matches.",
        {
            "pattern": {"type": "string", "description": "Extended regular expression."},
            "path": {"type": "string", "description": "Directory or file to limit the search to, or '.'."},
        },
        ["pattern", "path"],
    ),
    _tool(
        "find_references",
        "Find every place a function, class or variable name is used, as a whole word.",
        {"symbol": {"type": "string", "description": "The exact name."}},
        ["symbol"],
    ),
    _tool(
        "list_dir",
        "List the tracked files under a directory.",
        {"path": {"type": "string", "description": "Repo-relative directory, or '.'."}},
        ["path"],
    ),
    _tool(
        "git_log",
        "Show recent commits that touched a file, newest first.",
        {"path": {"type": "string", "description": "Repo-relative path."}},
        ["path"],
    ),
    _tool(
        "old_version",
        "Read lines from a file as it was BEFORE this change, to compare old and new behaviour.",
        {
            "path": {"type": "string", "description": "Repo-relative path."},
            "start_line": {"type": "integer", "description": "First line, 1-based."},
            "end_line": {"type": "integer", "description": "Last line, inclusive."},
        },
        ["path", "start_line", "end_line"],
    ),
]


class RepoTools:
    def __init__(self, repo: Path, merge_base: str, max_chars: int) -> None:
        self._repo = repo.resolve()
        self._base = merge_base
        self._max = max_chars
        self._tracked_files: set[str] | None = None

    def _safe(self, rel: str) -> Path:
        path = (self._repo / rel).resolve()
        if not path.is_relative_to(self._repo):
            raise ValueError("path is outside the repository")
        if any(part in _BLOCKED or part.startswith(".env") for part in path.relative_to(self._repo).parts):
            raise ValueError("that path is not readable")
        return path

    def _clip(self, text: str) -> str:
        if len(text) <= self._max:
            return text or "(no output)"
        return text[: self._max] + f"\n… clipped, {len(text) - self._max} more characters. Narrow the request."

    @staticmethod
    def _number(lines: list[str], start: int) -> str:
        return "\n".join(f"{start + i:>5}  {line}" for i, line in enumerate(lines))

    async def _tracked(self, rel: str) -> bool:
        """Only files git knows about are readable: an untracked notes or secrets file never leaves the machine."""
        if self._tracked_files is None:
            listed = await git(self._repo, "ls-files", "-z", check=False)
            self._tracked_files = set(listed.split("\0"))
        return Path(rel).as_posix().removeprefix("./") in self._tracked_files

    async def run(self, name: str, args: dict[str, Any]) -> str:
        if name == "read_file":
            path = self._safe(args["path"])
            if not path.is_file() or not await self._tracked(str(path.relative_to(self._repo))):
                raise ValueError("no such tracked file")
            lines = path.read_text(errors="replace").splitlines()
            start = max(1, int(args["start_line"]))
            end = min(len(lines), int(args["end_line"]), start + 199)
            return self._clip(self._number(lines[start - 1 : end], start) + f"\n({len(lines)} lines in file)")
        if name == "old_version":
            self._safe(args["path"])
            text = await git(self._repo, "show", f"{self._base}:{args['path']}", check=False)
            if not text:
                return "(the file did not exist before this change)"
            lines = text.splitlines()
            start = max(1, int(args["start_line"]))
            end = min(len(lines), int(args["end_line"]), start + 199)
            return self._clip(self._number(lines[start - 1 : end], start))
        if name == "grep":
            self._safe(args["path"])
            out = await git(self._repo, "grep", "-n", "-I", "-E", "-e", args["pattern"], "--", args["path"],
                            ":!.env*", check=False)
            return self._clip(out)
        if name == "find_references":
            out = await git(self._repo, "grep", "-n", "-I", "-w", "-F", "-e", args["symbol"], "--", ".",
                            ":!.env*", check=False)
            return self._clip(out)
        if name == "list_dir":
            self._safe(args["path"])
            return self._clip(await git(self._repo, "ls-files", "--", args["path"], check=False))
        if name == "git_log":
            self._safe(args["path"])
            return self._clip(
                await git(self._repo, "log", "-n", "8", "--format=%h %ad %s", "--date=short", "--", args["path"],
                          check=False)
            )
        raise ValueError(f"unknown tool {name}")


def describe_call(name: str, args: dict[str, Any]) -> str:
    """One short line for the reasoning trace."""
    if name in ("read_file", "old_version"):
        prefix = "old " if name == "old_version" else ""
        return f"{prefix}{args.get('path')}:{args.get('start_line')}-{args.get('end_line')}"
    if name == "grep":
        return f"grep /{args.get('pattern')}/ in {args.get('path')}"
    if name == "find_references":
        return f"uses of {args.get('symbol')}"
    if name == "list_dir":
        return f"files in {args.get('path')}"
    return f"history of {args.get('path')}"
