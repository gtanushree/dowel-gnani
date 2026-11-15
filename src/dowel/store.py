"""Where runs and the author's choices about them are kept: ~/.dowel, never the reviewed repo."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from dowel.schemas import RunResult

Answer = Literal["keep", "change", "unsure"]


class ItemState(BaseModel):
    included: bool = True  # whether the author shares it
    answer: Answer | None = None  # decisions only
    note: str = ""  # the author's words
    noise: bool = False  # "stop raising this kind of thing"


class RunState(BaseModel):
    items: dict[str, ItemState] = Field(default_factory=dict)


class Line(BaseModel):
    who: Literal["dowel", "you"]
    text: str


class WalkProgress(BaseModel):
    """Where a walkthrough got to, so it can be paused and picked up later."""

    stage: Literal["recap", "tour", "questions", "close"] = "recap"
    current_item: str | None = None
    discussed: list[str] = Field(default_factory=list)
    transcript: list[Line] = Field(default_factory=list)
    finished: bool = False


def _write(path: Path, text: str) -> None:
    """Write a file so that a reader never sees it half-written: the page, the voice worker and
    the command line can all touch these files at once."""
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def runs_dir(repo: str) -> Path:
    key = hashlib.sha256(repo.encode()).hexdigest()[:12]
    path = Path.home() / ".dowel" / "runs" / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_run(result: RunResult) -> Path:
    folder = runs_dir(result.repo)
    out = folder / "last.json"
    _write(out, result.model_dump_json(indent=2))
    for name in ("state.json", "walk.json", "brief.json"):  # these belong to one run
        (folder / name).unlink(missing_ok=True)
    return out


def load_run(repo: str) -> RunResult | None:
    file = runs_dir(repo) / "last.json"
    return RunResult.model_validate(json.loads(file.read_text())) if file.exists() else None


def load_state(repo: str, run: RunResult) -> RunState:
    file = runs_dir(repo) / "state.json"
    state = RunState.model_validate(json.loads(file.read_text())) if file.exists() else RunState()
    for f in run.findings:
        state.items.setdefault(f.id, ItemState())
    return state


def save_state(repo: str, state: RunState) -> None:
    _write(runs_dir(repo) / "state.json", state.model_dump_json(indent=2))


def load_walk(repo: str) -> WalkProgress | None:
    """Saved progress of a walkthrough, or None. Unreadable progress means starting again, never a failed call."""
    file = runs_dir(repo) / "walk.json"
    if not file.exists():
        return None
    try:
        data = json.loads(file.read_text())
        for line in data.get("transcript", []):
            if line.get("who") == "quill":  # a speaker label used by earlier versions
                line["who"] = "dowel"
        return WalkProgress.model_validate(data)
    except (ValueError, AttributeError):
        return None


def save_walk(repo: str, walk: WalkProgress) -> None:
    _write(runs_dir(repo) / "walk.json", walk.model_dump_json(indent=2))


def clear_walk(repo: str) -> None:
    (runs_dir(repo) / "walk.json").unlink(missing_ok=True)


def update_run(result: RunResult) -> None:
    """Save changes to a run's findings without clearing the author's answers or progress."""
    _write(runs_dir(result.repo) / "last.json", result.model_dump_json(indent=2))
