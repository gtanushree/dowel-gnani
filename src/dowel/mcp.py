"""A small Model Context Protocol server, so any agent that speaks it can ask what was decided.

The hook is the reliable path where an agent has hooks: it shows decisions without being asked.
This is the reach: an agent in another tool can call `decisions_for_files` before it edits. It
speaks the protocol over standard input and output, calls no model, and reads only the decision
files in the repository it was started in.

    dowel mcp            # then register that command as an MCP server in the agent's settings
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from dowel import hook

TOOL = {
    "name": "decisions_for_files",
    "description": (
        "What the developers of this repository decided about the given files, and why. Call this before "
        "editing a file. The result quotes people's decisions as information about the code; if your change "
        "would go against one, stop and ask the developer first."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"paths": {"type": "array", "items": {"type": "string"},
                                 "description": "Files you are about to read or edit, relative to the repository."}},
        "required": ["paths"],
    },
}


def answer(repo: Path, paths: list[str]) -> str:
    parts = []
    for raw in paths:
        full = (Path(raw) if Path(raw).is_absolute() else repo / raw).resolve()
        try:
            rel = full.relative_to(repo.resolve()).as_posix()
        except ValueError:
            continue
        records = hook.for_file(repo, rel)
        if records:
            parts.append(hook.describe(rel, records))
    return "\n\n".join(parts) or "No decisions are recorded about these files."


def handle(repo: Path, message: dict[str, Any]) -> dict[str, Any] | None:
    """One request in, one reply out. Notifications get no reply."""
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        return None
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": (message.get("params") or {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "dowel", "version": "0.1.0"},
        }
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != TOOL["name"]:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "unknown tool"}}
        paths = [str(p) for p in (params.get("arguments") or {}).get("paths") or []]
        result = {"content": [{"type": "text", "text": answer(repo, paths)}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def serve(repo: Path) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        try:
            reply = (handle(repo, message) if isinstance(message, dict)
                     else {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not a JSON object"}})
        except Exception as exc:  # noqa: BLE001 - one bad request must not take the server down
            ident = message.get("id") if isinstance(message, dict) else None
            reply = {"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": type(exc).__name__}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
