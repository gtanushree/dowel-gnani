"""The server other agents can ask, the history reader, and what check does with guards and claims."""

import subprocess
from pathlib import Path

import pytest

from dowel import check, history, mcp
from dowel import decisions as files


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "pay.py").write_text("def charge(order):\n    return send(order)\n")
    git(root, "add", "."), git(root, "commit", "-qm", "start")
    return root


def test_any_agent_can_ask_what_was_decided(repo: Path) -> None:
    files.write(repo, files.Record(uid="a1", kind="decision", created=1794700000.0, stance="keep", path="pay.py",
                                   symbol="charge", title="Charges are sent once.", reason="Double charges cost refunds."))
    listed = mcp.handle(repo, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed["result"]["tools"][0]["name"] == "decisions_for_files"
    called = mcp.handle(repo, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": "decisions_for_files", "arguments": {"paths": ["pay.py", "other.py"]}}})
    text = called["result"]["content"][0]["text"]
    assert "Charges are sent once." in text and "Double charges cost refunds." in text
    nothing = mcp.handle(repo, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "decisions_for_files", "arguments": {"paths": ["other.py"]}}})
    assert "No decisions" in nothing["result"]["content"][0]["text"]
    assert mcp.handle(repo, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert mcp.handle(repo, {"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601


def test_a_fix_whose_lines_were_later_removed_is_found_with_the_commit_that_removed_them(repo: Path) -> None:
    (repo / "pay.py").write_text("def charge(order):\n    if order.id in already_charged:\n"
                                 "        return already_charged[order.id]\n    return send(order)\n")
    git(repo, "commit", "-qam", "Fix double charge when a webhook is retried")
    (repo / "notes.py").write_text("x = 1\n")
    git(repo, "add", "."), git(repo, "commit", "-qm", "Add notes")
    assert history.find(repo) == []  # the fix is still in place

    (repo / "pay.py").write_text("def charge(order):\n    return send(order)\n")
    git(repo, "commit", "-qam", "Simplify charge")
    (found,) = history.find(repo)
    assert found.fix_subject.startswith("Fix double charge") and found.file == "pay.py"
    assert "if order.id in already_charged:" in found.gone and found.removed_by.endswith("Simplify charge")


def test_a_failing_guard_and_a_claim_that_tests_pass_both_stop_the_change(repo: Path) -> None:
    (repo / ".dowel/guards").mkdir(parents=True)
    (repo / ".dowel/guards/test_once.py").write_text(
        "from pay import charge\n\n\ndef test_once():\n    assert charge.__code__.co_argcount == 1, 'Charges take one order.'\n")
    git(repo, "add", "."), git(repo, "commit", "-qm", "guard")
    git(repo, "switch", "-qc", "work")
    base = git(repo, "merge-base", "main", "HEAD").strip()
    assert check.gather(repo, base).problems == []

    (repo / "pay.py").write_text("def charge(order, again):\n    return send(order)\n")
    git(repo, "commit", "-qam", "Refactor charge. All tests pass.")
    kinds = [p.kind for p in check.gather(repo, base).problems]
    assert kinds == ["guard", "claim"]
    guard = check.gather(repo, base).problems[0]
    assert any("Charges take one order." in line for line in guard.lines)


def test_a_question_the_prompt_already_answers_is_marked_not_dropped() -> None:
    import asyncio

    from test_memory import finding

    from dowel import asked

    covered = finding("h1", title="A missing config file now falls back to defaults.")
    covered.question = "Start anyway, or stop like before?"
    other = finding("h2", title="Retries have no upper limit.")
    issue = finding("h3", kind="issue", title="Signing is skipped.")

    async def yes(state: str, _statement: str) -> float:
        return 0.93 if "Start anyway" in state else 0.1

    prompt = "Add retries to webhooks.\nIf the config file is missing just start on defaults, staging has none."
    notes = asyncio.run(asked.mark_answered([covered, other, issue], prompt, yes))
    assert notes == ["your prompt may already answer: A missing config file now falls back to defaults."]
    assert covered.kind == "decision" and covered.memory == "your prompt may already answer this"
    assert 'From your prompt: "If the config file is missing just start on defaults, staging has none."' in covered.detail
    assert other.memory == "" and issue.memory == ""


def test_the_brief_is_posted_once_and_then_updated_in_place() -> None:
    from dowel import github

    calls: list[tuple[str, str]] = []
    comments: list[dict] = [{"id": 7, "body": "someone else's comment"}]

    def http(method: str, url: str, body):
        calls.append((method, url.split("/repos/acme/pay")[1]))
        if method == "GET":
            return comments
        if method == "POST":
            comments.append({"id": 9, "body": body["body"]})
        return {}

    env = {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "acme/pay", "GITHUB_REF": "refs/pull/482/merge"}
    assert github.post_brief("# Brief", env=env, http=http) == "created"
    assert github.post_brief("# Brief, again", env=env, http=http) == "updated"
    assert calls[-1] == ("PATCH", "/issues/comments/9") and calls[1] == ("POST", "/issues/482/comments")
    with pytest.raises(ValueError, match="pull request"):
        github.post_brief("x", env={"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "acme/pay", "GITHUB_REF": "refs/heads/main"}, http=http)
    with pytest.raises(ValueError, match="repository name"):
        github.upsert_comment("acme/pay/../../evil", 1, "x", http)
