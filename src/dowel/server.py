"""Local server for the proof sheet (and later the walkthrough). Binds to localhost only."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dowel import export, memory, store
from dowel.config import VoiceSettings
from dowel.gitops import changed_lines, git, run_git
from dowel.store import Answer

WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


_LOCAL = {"127.0.0.1", "localhost", "::1"}


Current = Callable[[], tuple[Any, Any]]


class ItemPatch(BaseModel):
    included: bool | None = None
    answer: Answer | None = None
    clear_answer: bool = False
    note: str | None = None
    noise: bool | None = None


def _local_only(app: FastAPI) -> None:
    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Binding to localhost is not enough: a web page elsewhere can still aim a browser at this port.

        Requests must name this machine as the host, and anything sent from a page must come from this one.
        """
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        origin = request.headers.get("origin")
        if host not in _LOCAL or (origin and (urlsplit(origin).hostname or "") not in _LOCAL):
            return JSONResponse({"detail": "This page only answers on this machine."}, status_code=403)
        return await call_next(request)


def _review_routes(app: FastAPI, repo: str, current: Current, payload: Callable[[], dict]) -> None:
    """The saved review: read it, answer its questions, copy it out."""
    @app.get("/api/run")
    def get_run() -> dict:
        return payload()

    @app.patch("/api/items/{item_id}")
    def patch_item(item_id: str, patch: ItemPatch) -> dict:
        run, state = current()
        if item_id not in state.items:
            raise HTTPException(404, "No such item in this run.")
        item = state.items[item_id]
        if patch.included is not None:
            item.included = patch.included
        if patch.clear_answer:
            item.answer = None
        elif patch.answer is not None:
            item.answer = patch.answer
        if patch.note is not None:
            item.note = patch.note[:2000]
        if patch.noise is not None:
            item.noise = patch.noise
            if patch.noise:
                item.included = False
        store.save_state(repo, state)
        memory.remember(repo, run, item_id, item, given="typed" if item.note.strip() else "ticked")
        return payload()

    @app.get("/api/export/summary", response_class=PlainTextResponse)
    def get_summary() -> str:
        return export.summary(*current())

    @app.get("/api/export/prompt", response_class=PlainTextResponse)
    def get_prompt() -> str:
        return export.agent_prompt(*current())


def _change_routes(app: FastAPI, repo: str, current: Current) -> None:
    """What needs no model: the change as a map, the reviewer's brief, and every decision on record."""
    def _merge_base() -> str:
        run, _ = current()
        return run_git(Path(repo), "merge-base", run.base, "HEAD").strip() or run.base

    # Plain functions, not async ones: they run git and may run the guard tests, and as plain
    # functions the server gives each its own thread instead of freezing every other request.
    @app.get("/api/change")
    def get_change() -> dict:
        """The change as a map: what was asked for, what was not, and what needs a person. No model."""
        from dowel import check, scope

        base = _merge_base()
        root = Path(repo)
        sc = scope.build(root, base, store.runs_dir(str(root.resolve())) / "prompts.jsonl")
        report = check.gather(root, base)
        return {
            "asked": sc.asked[:400], "source": sc.source,
            "files": [{"path": e.path, "label": e.label, "why": e.why, "added": e.added, "removed": e.removed}
                      for e in sc.entries],
            "problems": [{"kind": p.kind, "where": p.where, "what": p.what, "lines": list(p.lines[:4])}
                         for p in report.problems],
            "notes": report.notes,
        }

    @app.get("/api/export/brief", response_class=PlainTextResponse)
    def get_brief() -> str:
        from dowel import brief

        run, _ = current()
        return brief.build(Path(repo), _merge_base(), run.base)

    @app.get("/api/decisions")
    def get_decisions() -> dict:
        """Every decision on record for this repository, with where its code is now and whether a test guards it."""
        from dowel.memory import Memory

        root = Path(repo)
        from dowel import guard

        memory = Memory(root)
        try:
            rows = [{
                "id": d.id, "title": d.title, "reason": d.reason, "stance": d.stance, "given": d.given, "day": d.day,
                "file": where.file or d.file, "symbol": where.symbol or d.symbol, "line": where.first,
                "state": where.state, "moved": where.how, "shared": bool(d.uid),
                "guarded": guard.exists(root, d.uid, where.symbol or d.symbol), "held_back": bool(d.suspect),
            } for d, where in memory.located() if d.bug_class != "test_weakened"]
            clashes = [[a.id, b.id] for a, b in memory.conflicts()]
        finally:
            memory.close()
        return {"decisions": rows, "conflicts": clashes}


def _talk_routes(app: FastAPI, repo: str, current: Current) -> None:
    """Starting and resuming the spoken walkthrough."""
    @app.get("/api/talk/progress")
    def talk_progress() -> dict:
        """Where the last walkthrough got to, for the resume screen and the history."""
        run, state = current()
        walk = store.load_walk(repo)
        shown = [f for f in run.findings if f.kind != "background" and not state.items[f.id].noise]
        shown.sort(key=lambda f: f.kind != "decision")  # the order the walkthrough takes them in
        open_items = [f for f in shown if state.items[f.id].answer is None]
        return {
            "started": bool(walk and walk.transcript),
            "finished": bool(walk and walk.finished and not open_items),
            "total": len(shown),
            "answered": len(shown) - len(open_items),
            "next": open_items[0].title if open_items else None,
            "transcript": [line.model_dump() for line in walk.transcript] if walk else [],
        }

    @app.post("/api/talk/token")
    def talk_token(fresh: bool = False) -> dict:
        """A short-lived pass into a brand-new room, with the walkthrough agent invited."""
        from livekit import api

        current()
        voice = VoiceSettings()
        if voice.missing():  # checked first: a start that cannot happen must not cost anyone their answers
            raise HTTPException(503, "Voice is not set up: missing " + ", ".join(voice.missing()))
        if fresh:
            # Starting over means answering again: clear the answers, keep what was ticked or silenced.
            _, state = current()
            for item in state.items.values():
                item.answer, item.note = None, ""
            store.save_state(repo, state)
            store.clear_walk(repo)
        room = f"dowel-{uuid.uuid4()}"  # never reused: the agent invitation only applies to a new room
        token = (
            api.AccessToken(voice.livekit_key, voice.livekit_secret)
            .with_identity(f"author-{uuid.uuid4().hex[:8]}")
            .with_ttl(timedelta(minutes=15))
            .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                                         can_publish_data=True))
            .with_room_config(api.RoomConfiguration(agents=[
                api.RoomAgentDispatch(agent_name=voice.agent_name, metadata=json.dumps({"repo": repo, "fresh": fresh}))
            ]))
        )
        return {"url": voice.livekit_url, "token": token.to_jwt(), "room": room}


def _code_route(app: FastAPI, repo: str, current: Current) -> None:
    @app.get("/api/code")
    async def code(file: str, start: int, end: int) -> dict:
        """Lines of a file for the code viewer, with the lines this change added marked."""
        run, _ = current()
        root = Path(repo).resolve()
        path = (root / file).resolve()
        parts = path.relative_to(root).parts if path.is_relative_to(root) else (".env",)
        tracked = bool((await git(root, "ls-files", "--", file, check=False)).strip())
        if any(part == ".git" or part.startswith(".env") for part in parts) or not path.is_file() or not tracked:
            raise HTTPException(404, "That file cannot be shown.")
        lines = path.read_text(errors="replace").splitlines()
        start, end = max(1, start), min(len(lines), end, start + 80)
        merge_base = (await git(root, "merge-base", run.base, "HEAD", check=False)).strip() or run.base
        diff = await git(root, "diff", "--unified=0", "--no-color", merge_base, "--", file, check=False)
        added = {n for lo, hi in changed_lines(diff).get(file, []) for n in range(lo, hi + 1)}
        return {"file": file, "lines": [{"n": n, "text": lines[n - 1], "added": n in added}
                                         for n in range(start, end + 1)]}


def _page_routes(app: FastAPI) -> None:
    """The built page, when there is one."""
    if WEB_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            return FileResponse(WEB_DIST / "index.html")


def create_app(repo: str) -> FastAPI:
    app = FastAPI(title="Dowel", docs_url=None, redoc_url=None)

    def current():
        run = store.load_run(repo)
        if run is None:
            raise HTTPException(404, "No saved run for this repository. Run `dowel` first.")
        return run, store.load_state(repo, run)

    def payload() -> dict:
        run, state = current()
        return {
            "run": run.model_dump(),
            "state": state.model_dump(),
            "labels": {f.id: export.label(f, state.items[f.id]) for f in run.findings},
            "why": export.why_note(run, state),
        }

    _local_only(app)
    _review_routes(app, repo, current, payload)
    _change_routes(app, repo, current)
    _talk_routes(app, repo, current)
    _code_route(app, repo, current)
    _page_routes(app)
    return app
