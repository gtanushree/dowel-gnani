"""Putting the reviewer's brief on a pull request.

No GitHub App is needed. A workflow in the repository runs `dowel brief --github`, which posts the
brief as one comment using the token GitHub gives every workflow run. The same comment is updated
on each push, so a pull request carries one brief, not a pile of them. Decisions are files in the
repository, so the runner has them without any setup.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from collections.abc import Callable
from typing import Any

MARK = "<!-- dowel-brief -->"
Http = Callable[[str, str, dict[str, Any] | None], Any]
"""(method, url, body) -> decoded JSON reply."""


def _http(token: str) -> Http:
    def call(method: str, url: str, body: dict[str, Any] | None) -> Any:
        if not url.startswith("https://api.github.com/"):
            raise ValueError("refusing to send the token anywhere but api.github.com")
        request = urllib.request.Request(
            url, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"authorization": f"Bearer {token}", "accept": "application/vnd.github+json",
                     "content-type": "application/json", "user-agent": "dowel"})
        with urllib.request.urlopen(request, timeout=15) as reply:
            return json.load(reply)
    return call


def pull_number(env: dict[str, str] | None = None) -> int | None:
    """The pull request a workflow run is for, from the environment GitHub provides."""
    env = dict(os.environ) if env is None else env
    match = re.fullmatch(r"refs/pull/(\d+)/(merge|head)", env.get("GITHUB_REF", ""))
    return int(match.group(1)) if match else None


def upsert_comment(repository: str, pull: int, body: str, http: Http) -> str:
    """Post the brief, or update the one already there. Returns "created" or "updated"."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError(f"not a repository name: {repository}")
    base = f"https://api.github.com/repos/{repository}"
    text = f"{MARK}\n{body}"
    page = 1
    while page <= 10:
        comments = http("GET", f"{base}/issues/{pull}/comments?per_page=100&page={page}", None)
        for mine in (c for c in comments if (c.get("body") or "").startswith(MARK)):
            try:
                http("PATCH", f"{base}/issues/comments/{mine['id']}", {"body": text})
            except OSError:
                continue  # not ours to edit: someone else's comment carries the same marker
            return "updated"
        if len(comments) < 100:
            break
        page += 1
    http("POST", f"{base}/issues/{pull}/comments", {"body": text})
    return "created"


def post_brief(body: str, pull: int | None = None, env: dict[str, str] | None = None, http: Http | None = None) -> str:
    env = dict(os.environ) if env is None else env
    token, repository = env.get("GITHUB_TOKEN", ""), env.get("GITHUB_REPOSITORY", "")
    number = pull or pull_number(env)
    if not token or not repository or number is None:
        raise ValueError("needs GITHUB_TOKEN, GITHUB_REPOSITORY and a pull request (run inside a pull_request "
                         "workflow, or pass --pr)")
    return upsert_comment(repository, number, body, http or _http(token))
