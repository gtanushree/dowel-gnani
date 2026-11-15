"""The exports share only what the author ticked and never judge the author."""

from fastapi.testclient import TestClient

from dowel import export, store
from dowel.schemas import Evidence, Finding, Hypothesis, IntentGuess, RunResult, Usage, Verdict
from dowel.server import create_app
from dowel.store import ItemState, RunState


def _finding(fid: str, kind: str, title: str, question: str = "") -> Finding:
    h = Hypothesis(id=fid, statement=title, bug_class="other", file="a.py", line=3, check="look")
    v = Verdict(status="confirmed", reasoning="r", trigger="t", question_for_author="",
                evidence=[Evidence(file="a.py", start_line=3, end_line=3, quote="x = 1")])
    return Finding(id=fid, kind=kind, title=title, file="a.py", line=3, hypothesis=h, verdict=v, question=question)


def _run(repo: str = "/tmp/dowel-test-repo") -> RunResult:
    return RunResult(
        repo=repo, base="main", head="feature", files_changed=1,
        intent=IntentGuess(summary="Adds retries.", claims=[], confidence="medium"),
        findings=[
            _finding("h1", "issue", "Failed deliveries are counted as sent."),
            _finding("h2", "decision", "A missing config file now starts the app.", "Keep that?"),
            _finding("h3", "decision", "Signature checking is off by default.", "Keep that?"),
            _finding("h4", "background", "A minor thing."),
        ],
        dropped=[], stages=[], usage=Usage(), seconds=1.0,
    )


def test_summary_groups_by_what_the_author_did() -> None:
    state = RunState(items={
        "h1": ItemState(),
        "h2": ItemState(answer="unsure", note="not sure about staging"),
        "h3": ItemState(answer="change", note="keep it on"),
    })
    text = export.summary(_run(), state)
    assert "Spend your review time on a.py:3: A missing config file now starts the app." in text
    assert 'Signature checking is off by default. This will change. "keep it on"' in text
    assert "Worth a second look" in text and "Found by Dowel" in text
    assert "A minor thing" not in text
    for word in ("unsure", "confirmed", "contradicted", "failed to"):
        assert word not in text.lower().replace("failed deliveries", "")


def test_unticked_and_noise_items_are_left_out() -> None:
    state = RunState(items={"h1": ItemState(included=False), "h2": ItemState(noise=True), "h3": ItemState()})
    text = export.summary(_run(), state)
    assert "counted as sent" not in text and "missing config" not in text
    assert "Signature checking" in text


def test_agent_prompt_has_issues_and_changes_only() -> None:
    state = RunState(items={"h1": ItemState(), "h2": ItemState(answer="keep"),
                            "h3": ItemState(answer="change", note="keep it on")})
    text = export.agent_prompt(_run(), state)
    assert "1. Failed deliveries are counted as sent." in text and "Evidence: `x = 1`" in text
    assert "What I want instead: keep it on" in text
    assert "missing config" not in text


def test_server_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store.Path, "home", lambda: tmp_path)
    run = _run(str(tmp_path / "repo"))
    store.save_run(run)
    client = TestClient(create_app(run.repo), base_url="http://127.0.0.1:4517")
    assert client.get("/api/run").json()["labels"]["h2"] == "for you"
    body = client.patch("/api/items/h2", json={"answer": "keep", "note": "intended"}).json()
    assert body["labels"]["h2"] == "you decided" and body["why"]["decided"]
    client.patch("/api/items/h1", json={"noise": True})
    assert "counted as sent" not in client.get("/api/export/summary").text
    assert client.patch("/api/items/nope", json={"included": False}).status_code == 404


def test_code_endpoint_marks_added_lines_and_hides_secrets(tmp_path, monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(store.Path, "home", lambda: tmp_path / "home")
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                       check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    (repo / "a.py").write_text("one\ntwo\n")
    (repo / ".env").write_text("SECRET=1\n")
    git("add", "a.py")
    git("commit", "-q", "-m", "base")
    git("switch", "-q", "-c", "feature")
    (repo / "a.py").write_text("one\nnew\ntwo\n")
    git("commit", "-q", "-am", "add a line")

    store.save_run(_run(str(repo)))
    client = TestClient(create_app(str(repo)), base_url="http://127.0.0.1:4517")
    lines = client.get("/api/code", params={"file": "a.py", "start": 1, "end": 10}).json()["lines"]
    assert [(line["text"], line["added"]) for line in lines] == [("one", False), ("new", True), ("two", False)]
    assert client.get("/api/code", params={"file": ".env", "start": 1, "end": 5}).status_code == 404
    assert client.get("/api/code", params={"file": "../x", "start": 1, "end": 5}).status_code == 404


def test_server_refuses_callers_that_are_not_this_machine(tmp_path) -> None:
    client = TestClient(create_app(str(tmp_path)), base_url="http://evil.example")
    assert client.get("/api/run").status_code == 403
    local = TestClient(create_app(str(tmp_path)), base_url="http://127.0.0.1:4517")
    assert local.get("/api/run", headers={"origin": "http://evil.example"}).status_code == 403
    assert local.get("/api/run").status_code == 404  # reaches the app: there is simply no saved run


def test_progress_saved_under_the_old_name_still_loads_and_garbage_starts_fresh(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    from dowel import store

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    file = store.runs_dir("/some/repo") / "walk.json"
    file.write_text('{"stage": "questions", "transcript": [{"who": "quill", "text": "Hi."}, {"who": "you", "text": "Hey."}]}')
    assert [line.who for line in store.load_walk("/some/repo").transcript] == ["dowel", "you"]
    file.write_text('{"transcript": [{"who": "someone else", "text": "x"}]}')
    assert store.load_walk("/some/repo") is None
    file.write_text("not json")
    assert store.load_walk("/some/repo") is None
