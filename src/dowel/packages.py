"""Dependencies a change adds that do not exist, or appeared only days ago.

Coding agents sometimes name a package that was never published. Installing it then fails, or
worse, installs something an attacker registered under that name. Each newly added dependency is
looked up in its public registry. Python (PyPI) and JavaScript (npm) for now.

The lookup sends only the package name to the registry it would be installed from. If the
registry cannot be reached, that is noted and nothing is flagged.
"""

from __future__ import annotations

import calendar
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

YOUNG_DAYS = 14

_REQ = re.compile(r"^\+\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(==|>=|<=|~=|!=|>|<|$|;|#)")
_TOML = re.compile(r"^\+\s*\"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(==|>=|<=|~=|!=|>|<)")
_NPM = re.compile(r"^\+\s*\"((?:@[\w.-]+/)?[\w.-]+)\"\s*:\s*\"(?:[\^~<>=*\d]|latest|workspace:|npm:)")

Lookup = Callable[[str, str], "tuple[bool, float | None] | None"]
"""(registry, name) -> (exists, first published as a timestamp), or None if the registry could not be asked."""


@dataclass(frozen=True)
class Flagged:
    name: str
    registry: str  # pypi | npm
    file: str
    problem: str


_NOT_PACKAGES = {"python", "name", "version", "node", "npm", "yarn", "pnpm", "engines", "main", "type", "license"}


def added(diff: str) -> list[tuple[str, str, str]]:
    """(registry, package, file) for each dependency the change newly adds.

    A package whose line was also removed in the same file is a version change, not a new
    dependency, and is left alone. Entries that point at a workspace, a file or an alias are never
    looked up: they are not public packages, and their names are nobody else's business.
    """
    found: list[tuple[str, str, str]] = []
    dropped: set[tuple[str, str]] = set()
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[4:].strip().removeprefix("b/")
            continue
        if line.startswith(("---", "+++")) or not line.startswith(("+", "-")):
            continue
        name = current.rsplit("/", 1)[-1]
        probe = "+" + line[1:]
        match = registry = None
        if name.endswith(".txt") and ("requirements" in current or "constraints" in name):
            match, registry = _REQ.match(probe), "pypi"
        elif name == "pyproject.toml":
            match, registry = _TOML.match(probe), "pypi"
        elif name == "package.json" and not re.search(r'":\s*"(workspace:|npm:|file:|link:|git\+|https?:)', probe):
            match, registry = _NPM.match(probe), "npm"
        if not match or match.group(1).lower() in _NOT_PACKAGES:
            continue
        if line.startswith("-"):
            dropped.add((current, match.group(1)))
        else:
            found.append((registry or "", match.group(1), current))
    return list(dict.fromkeys(item for item in found if (item[2], item[1]) not in dropped))


def ask_registry(registry: str, name: str) -> tuple[bool, float | None] | None:
    url = (f"https://pypi.org/pypi/{name}/json" if registry == "pypi"
           else f"https://registry.npmjs.org/{name.replace('/', '%2F')}")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "dowel"}), timeout=6) as reply:
            data = json.load(reply)
    except urllib.error.HTTPError as exc:
        return (False, None) if exc.code == 404 else None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    stamps: list[str] = []
    if registry == "pypi":
        stamps = [f["upload_time_iso_8601"] for files in data.get("releases", {}).values() for f in files
                  if f.get("upload_time_iso_8601")]
    elif isinstance(data.get("time"), dict) and data["time"].get("created"):
        stamps = [data["time"]["created"]]
    first = min((calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) for s in stamps), default=None)
    return True, first


def check(diff: str, lookup: Lookup = ask_registry, now: float | None = None) -> tuple[list[Flagged], list[str]]:
    """Returns what to flag, and notes about anything that could not be looked up."""
    flagged: list[Flagged] = []
    notes: list[str] = []
    for registry, name, file in added(diff):
        got = lookup(registry, name)
        if got is None:
            notes.append(f"could not reach {registry} to look up {name}")
            continue
        exists, first = got
        if not exists:
            flagged.append(Flagged(name, registry, file, f"no package called {name} exists on {registry}"))
        elif first is not None and ((now or time.time()) - first) < YOUNG_DAYS * 86400:
            days = max(0, int(((now or time.time()) - first) // 86400))
            flagged.append(Flagged(name, registry, file, f"{name} was first published {days} days ago"))
    return flagged, notes
