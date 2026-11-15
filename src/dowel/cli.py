"""The `dowel` command."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.text import Text

from dowel import store
from dowel.config import Settings
from dowel.gitops import GitError
from dowel.llm import LLMError
from dowel.pipeline import Event, review
from dowel.schemas import Finding, RunResult

app = typer.Typer(add_completion=False, help="Keeps what you decided about a change, and holds later changes to it.", no_args_is_help=False)
console = Console(highlight=False)

AMBER = "#E0B36A"
MUTED = "#9A9892"


def _clock(seconds: float) -> str:
    return f"{int(seconds)}s" if seconds < 60 else f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def _row(f: Finding, text: str) -> Text:
    line = Text()
    line.append(f"{f.priority} ", style=MUTED)
    line.append(f"{f.file}:{f.line}".ljust(30), style=AMBER)
    line.append(" " + text)
    return line


def _print_result(result: RunResult, show_trace: bool) -> None:
    issues, decisions = result.issues, result.decisions
    if not issues and not decisions:
        console.print(f"\nNothing here needs you. {len(result.dropped)} ideas checked and dropped.", style=MUTED)
        return
    if issues:
        console.print("\nissue", style=MUTED)
        for f in issues:
            console.print(_row(f, f.title))
            if f.detail:
                console.print(Text(" " * 31 + f.detail, style=MUTED))
            if show_trace:
                _print_trace(f)
    if decisions:
        console.print("\nfor you", style=MUTED)
        for f in decisions:
            console.print(_row(f, f"{f.title} {f.question}".strip()))
            if f.detail:
                console.print(Text(" " * 31 + f.detail, style=MUTED))
            if show_trace:
                _print_trace(f)


def _print_trace(f: Finding) -> None:
    def line(label: str, value: str) -> None:
        console.print(Text(f"    {label:<16}", style=MUTED).append(value, style="default"))

    line("idea", f.hypothesis.statement)
    if f.searched:
        line("looked at", ", ".join(f.searched[:6]))
    for e in f.verdict.evidence[:2]:
        line("evidence", f"{e.file}:{e.start_line}  {e.quote.strip().splitlines()[0][:100]}")
    if f.review:
        line("tried to refute", f.review.refutation_attempt)
    console.print()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    path: Annotated[Path, typer.Option("--repo", help="Repository to review.")] = Path("."),
    base: Annotated[str | None, typer.Option(help="Branch to compare against. Default: the main branch.")] = None,
    fast: Annotated[bool, typer.Option(help="Fewer, quicker checks.")] = False,
    trace: Annotated[bool, typer.Option(help="Show the reasoning behind each finding.")] = False,
    max_cost: Annotated[float | None, typer.Option(help="Stop the review at this many dollars.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full result as JSON.")] = False,
) -> None:
    """Review the current branch against its base."""
    if ctx.invoked_subcommand:
        return
    settings = Settings().fast() if fast else Settings()

    def emit(event: Event) -> None:
        if as_json or event.kind == "finding":
            return
        if event.kind == "check":
            console.print(Text("  · " + event.text[:96], style=MUTED))
        elif event.kind == "stage":
            last = event.text.startswith("done")
            line = Text(event.text.ljust(58), style="default" if last else MUTED)
            line.append(_clock(event.seconds).rjust(8), style="default" if last else MUTED)
            console.print(line)
        else:
            console.print(event.text, style=MUTED)

    try:
        result = asyncio.run(review(path.resolve(), base, settings, emit, max_cost))
    except (GitError, LLMError) as exc:
        console.print(f"dowel: {exc}", style="red")
        raise typer.Exit(1) from exc

    out = store.save_run(result)

    if as_json:
        print(result.model_dump_json(indent=2))
        return
    _print_result(result, trace)
    cost = result.usage.cost_usd
    console.print(
        f"\n{len(result.findings) + len(result.dropped)} ideas checked · {len(result.dropped)} dropped · "
        f"about ${cost:.2f} · saved to {out}",
        style=MUTED,
    )
    if result.decisions:
        console.print("talk it through:  dowel talk     or answer on the page:  dowel open", style=MUTED)


@app.command()
def show(
    path: Annotated[Path, typer.Option("--repo", help="Repository the run was for.")] = Path("."),
    sample: Annotated[bool, typer.Option("--sample", help="Show a saved review of a small example service. "
                                         "Needs no keys and no repository.")] = False,
) -> None:
    """Show the last run for this repository, with the reasoning behind each finding."""
    from dowel.gitops import repo_root

    if sample:
        from dowel import decisions as decision_files

        folder = Path(__file__).parent / "sample"
        result = RunResult.model_validate_json((folder / "last.json").read_text())
        console.print("A saved review of an example: an agent's change adding retries to a payments service.\n",
                      style=MUTED)
        if result.intent.summary:
            console.print(result.intent.summary + "\n")
        _print_result(result, show_trace=True)
        console.print("\nWhat the developer then decided, as recorded in the repository:", style=MUTED)
        for file in sorted((folder / "decisions").glob("*.md")):
            record = decision_files.parse(file.read_text())
            if record is not None:
                verdict = "keep" if record.stance == "keep" else "change"
                console.print(Text(f"  {verdict:6} ", style=AMBER) + Text(f"{record.title}"))
                console.print(Text(f'         "{record.reason}" · {record.path} · {record.symbol}', style=MUTED))
        return
    try:
        repo = asyncio.run(repo_root(path.resolve()))
    except GitError as exc:
        console.print(f"dowel: {exc}", style="red")
        raise typer.Exit(1) from exc
    result = store.load_run(str(repo))
    if result is None:
        console.print("No saved run for this repository yet. Run `dowel` first.", style=MUTED)
        raise typer.Exit(1)
    if result.intent.summary:
        console.print(result.intent.summary + "\n")
    _print_result(result, show_trace=True)
    if result.stages or result.usage.by_model:
        times = " · ".join(f"{t.name} {t.seconds:.0f}s" for t in result.stages)
        spend = " · ".join(f"{name.removeprefix('claude-')} ${cost:.2f}" for name, cost in
                           sorted(result.usage.by_model.items(), key=lambda kv: -kv[1]))
        console.print(f"\nwhere the time went: {times}" + (f"\nwhere the money went: {spend}" if spend else ""),
                      style=MUTED)
    if result.dropped:
        console.print(f"\n{len(result.dropped)} ideas checked and dropped", style=MUTED)
        for d in result.dropped:
            console.print(Text(f"  {d.hypothesis.statement[:90]}", style=MUTED))
            console.print(Text(f"    · {d.reason[:160]}", style=MUTED))


@app.command("open")
def open_sheet(
    path: Annotated[Path, typer.Option("--repo", help="Repository the run was for.")] = Path("."),
    port: Annotated[int, typer.Option(help="Local port.")] = 4517,
    browser: Annotated[bool, typer.Option(help="Open the page in your browser.")] = True,
) -> None:
    """Open the proof sheet for the last run: answer the questions, choose what to share."""
    _serve(path, port, browser, voice=False)


@app.command()
def talk(
    path: Annotated[Path, typer.Option("--repo", help="Repository the run was for.")] = Path("."),
    port: Annotated[int, typer.Option(help="Local port.")] = 4517,
    browser: Annotated[bool, typer.Option(help="Open the page in your browser.")] = True,
) -> None:
    """Talk the last run through with Dowel, with the code on screen."""
    _serve(path, port, browser, voice=True)


def _serve(path: Path, port: int, browser: bool, voice: bool) -> None:
    import webbrowser

    import uvicorn

    from dowel.gitops import repo_root
    from dowel.server import WEB_DIST, create_app

    try:
        repo = asyncio.run(repo_root(path.resolve()))
    except GitError as exc:
        console.print(f"dowel: {exc}", style="red")
        raise typer.Exit(1) from exc
    if store.load_run(str(repo)) is None:
        console.print("No saved run for this repository yet. Run `dowel` first.", style=MUTED)
        raise typer.Exit(1)
    if not WEB_DIST.is_dir():
        console.print("The web page is not built yet. Run `npm install && npm run build` in web/.", style="red")
        raise typer.Exit(1)
    worker = None
    if voice:
        import subprocess
        import sys

        from dowel.config import VoiceSettings

        missing = VoiceSettings().missing()
        if missing:
            console.print(f"Voice needs these keys in .env: {', '.join(missing)}", style="red")
            raise typer.Exit(1)
        log = store.runs_dir(str(repo)) / "voice.log"
        worker = subprocess.Popen(
            [sys.executable, "-m", "dowel.voice.worker", "start"],
            stdout=log.open("w"), stderr=subprocess.STDOUT,
        )
        console.print(f"voice worker started (log: {log})", style=MUTED)
        # If this command is stopped from outside, still take the worker down with it.
        import atexit
        import signal

        atexit.register(worker.terminate)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    url = f"http://127.0.0.1:{port}" + ("/talk" if voice else "")
    console.print(f"{'walkthrough' if voice else 'proof sheet'} at {url}  (ctrl-c to stop)", style=MUTED)
    if browser:
        webbrowser.open(url)
    try:
        uvicorn.run(create_app(str(repo)), host="127.0.0.1", port=port, log_level="warning")
    finally:
        if worker is not None:
            worker.terminate()


@app.command("check")
def check_cmd(
    repo_path: Annotated[Path, typer.Option("--repo", help="Repository.")] = Path("."),
    base: Annotated[str | None, typer.Option(help="Branch to compare against. Default: the main branch.")] = None,
    judge: Annotated[bool, typer.Option("--judge", help="Ask a model whether changed code still honours its "
                                        "decisions. Costs a few cents.")] = False,
    offline: Annotated[bool, typer.Option("--offline", help="Do not look up newly added packages in their "
                                          "registries.")] = False,
    accept: Annotated[int | None, typer.Option(help="Accept a weakened test by its number, with --because.")] = None,
    because: Annotated[str, typer.Option(help="Why. Recorded with the acceptance.")] = "",
) -> None:
    """Fail if this change weakens a test or touches a decision without anyone having looked.

    Exits 1 when something needs a person. Suitable as a step in any build.
    """
    from dowel import check
    from dowel.gitops import default_base, git, repo_root

    try:
        repo = asyncio.run(repo_root(repo_path.resolve()))
        merge_base = asyncio.run(git(repo, "merge-base", base or asyncio.run(default_base(repo)), "HEAD")).strip()
    except GitError as exc:
        console.print(f"dowel: {exc}", style="red")
        raise typer.Exit(2) from exc
    report = check.gather(Path(repo), merge_base)
    if accept is not None:
        if not 1 <= accept <= len(report.problems):
            console.print("No such number in the list.", style="red")
            raise typer.Exit(2)
        try:
            written = check.accept(Path(repo), report.problems[accept - 1], because)
        except ValueError as exc:
            console.print(f"dowel: {exc}", style="red")
            raise typer.Exit(2) from exc
        console.print(f"Recorded in {written}. Commit it with the change.", style=MUTED)
        return
    if not offline:
        from dowel import packages

        diff = asyncio.run(git(repo, "diff", "--unified=0", "--no-color", merge_base))
        flagged, could_not = packages.check(diff)
        report.notes += could_not
        report.problems += [check.Problem("package", f.file, f.problem + ". Check it is the package you meant.")
                            for f in flagged]
    if judge and any(p.kind == "decision" for p in report.problems):
        asyncio.run(check.judge(Path(repo), merge_base, report))
    for note in report.notes:
        console.print(note, style=MUTED)
    if not report.problems:
        console.print(f"Nothing needs a person. {len(report.changed)} files changed.", style=MUTED)
        return
    for n, p in enumerate(report.problems, 1):
        line = Text(f"{n:>2}  ", style=AMBER)
        line.append(p.where, style="bold")
        console.print(line)
        console.print(Text(f"    {p.what}"))
        for shown in p.lines[:4]:
            console.print(Text(f"      {shown}", style=MUTED))
        if p.kind == "test":
            console.print(Text(f'    if this is intended: dowel check --accept {n} --because "why"', style=MUTED))
        elif p.kind == "guard":
            console.print(Text("    the decision it guards has been undone; restore it, or retire the decision", style=MUTED))
        elif p.kind == "decision" and not judge:
            console.print(Text("    run `dowel check --judge`, or `dowel` for a full review", style=MUTED))
        elif p.kind == "decision":
            console.print(Text(f"    if you have changed your mind: dowel memory --forget decision:{p.decision.id} "
                               '--because "why"', style=MUTED))
    raise typer.Exit(1)


@app.command("brief")
def brief_cmd(
    repo_path: Annotated[Path, typer.Option("--repo", help="Repository.")] = Path("."),
    base: Annotated[str | None, typer.Option(help="Branch to compare against. Default: the main branch.")] = None,
    out: Annotated[Path | None, typer.Option(help="Write the brief to this file instead of printing it.")] = None,
    slack: Annotated[bool, typer.Option("--slack", help="Post it to the Slack webhook in DOWEL_SLACK_WEBHOOK.")] = False,
    github: Annotated[bool, typer.Option("--github", help="Post it as a comment on the pull request this "
                                         "workflow run is for, updating the one already there.")] = False,
    pr: Annotated[int | None, typer.Option(help="Pull request number, when not running in a workflow.")] = None,
) -> None:
    """Write the reviewer's brief for this change: what is unexplained, what was decided, what is open."""
    from dowel import brief
    from dowel.gitops import default_base, git, repo_root

    try:
        repo = asyncio.run(repo_root(repo_path.resolve()))
        against = base or asyncio.run(default_base(repo))
        merge_base = asyncio.run(git(repo, "merge-base", against, "HEAD")).strip()
    except GitError as exc:
        console.print(f"dowel: {exc}", style="red")
        raise typer.Exit(2) from exc
    text = brief.build(Path(repo), merge_base, against)
    if github:
        from dowel import github as gh

        try:
            done = gh.post_brief(text, pr)
        except (ValueError, OSError) as exc:
            console.print(f"dowel: could not post the brief: {exc}", style="red")
            raise typer.Exit(2) from exc
        console.print(f"Brief {done} on the pull request.", style=MUTED)
        return
    if slack:
        import json as jsonlib
        import urllib.request

        from dowel.config import slack_webhook

        url = slack_webhook()
        if not url or not url.startswith("https://hooks.slack.com/"):
            console.print("Set DOWEL_SLACK_WEBHOOK to a Slack incoming-webhook address first.", style="red")
            raise typer.Exit(2)
        body = jsonlib.dumps({"text": text.replace("**", "*")}).encode()
        with urllib.request.urlopen(urllib.request.Request(
                url, data=body, headers={"content-type": "application/json"}), timeout=10):
            pass
        console.print("Posted the brief to Slack.", style=MUTED)
        return
    if out is not None:
        out.write_text(text)
        console.print(f"Wrote {out}.", style=MUTED)
    else:
        print(text, end="")


@app.command("guard")
def guard_cmd(
    which: Annotated[str, typer.Argument(help="The decision to guard, such as decision:3 (see `dowel memory`), "
                                              "or `all` for every decision to keep that has no guard yet.")],
    repo_path: Annotated[Path, typer.Option("--repo", help="Repository.")] = Path("."),
) -> None:
    """Write a test that fails if this decision is undone. One model call; runs the test in a copy first."""
    from dowel import guard
    from dowel.config import Settings
    from dowel.gitops import repo_root
    from dowel.llm import Claude, Ledger
    from dowel.memory import Memory

    try:
        repo = Path(asyncio.run(repo_root(repo_path.resolve())))
    except GitError as exc:
        console.print(f"dowel: {exc}", style="red")
        raise typer.Exit(2) from exc
    memory = Memory(repo)
    try:
        if which == "all":
            todo = [(d, memory.locate(d)) for d in memory.decisions() if d.stance == "keep" and d.bug_class != "test_weakened"]
            todo = [(d, where) for d, where in todo if not guard.exists(repo, d.uid, where.symbol)]
            todo = [(d, where) for d, where in todo if where.file.endswith(".py") and where.symbol]
        else:
            number = which.removeprefix("decision:")
            decision = next((d for d in memory.decisions() if str(d.id) == number), None)
            if decision is None:
                console.print("No such decision. Run `dowel memory` to see their numbers.", style="red")
                raise typer.Exit(2)
            todo = [(decision, memory.locate(decision))]
    finally:
        memory.close()
    if which == "all":
        claude, model, wrote = Claude(Ledger(0.25 * max(1, len(todo)))), Settings().models.verify, 0
        for decision, where in todo:
            try:
                out = asyncio.run(guard.write(repo, decision, where, claude, model))
            except LLMError as exc:
                console.print(f"dowel: stopped: {exc}", style="red")
                break
            wrote += bool(out.written)
            console.print(Text(f"  {'guarded ' if out.written else 'no guard'}  ", style=AMBER if out.written else MUTED)
                          + Text(f"{decision.title[:70]}" + ("" if out.written else f"  ({out.refused.splitlines()[0][:80]})")))
        console.print(f"\n{wrote} of {len(todo)} decisions now have a guard test.", style=MUTED)
        return
    decision, where = todo[0]
    try:
        out = asyncio.run(guard.write(repo, decision, where, Claude(Ledger(0.50)), Settings().models.verify))
    except LLMError as exc:
        console.print(f"dowel: {exc}", style="red")
        raise typer.Exit(1) from exc
    if out.written:
        console.print(f"Wrote {out.written}. It passes now and fails when the decision is undone.\n"
                      "Commit it with the decision. Agents are refused edits to it.", style=MUTED)
    else:
        console.print(f"No guard test written: {out.refused}", style=AMBER)
        raise typer.Exit(1)


@app.command("mcp")
def mcp_cmd(repo_path: Annotated[Path, typer.Option("--repo", help="Repository.")] = Path(".")) -> None:
    """Serve this repository's decisions to any agent that speaks the Model Context Protocol."""
    from dowel import mcp
    from dowel.hook import repo_of

    mcp.serve(repo_of(repo_path.resolve()) or repo_path.resolve())


@app.command("history")
def history_cmd(
    repo_path: Annotated[Path, typer.Option("--repo", help="Repository.")] = Path("."),
    commits: Annotated[int, typer.Option(help="How many recent commits to read.")] = 300,
) -> None:
    """Find fixes in this repository's history that a later change undid. No model is called."""
    from dowel import history
    from dowel.gitops import repo_root

    try:
        repo = Path(asyncio.run(repo_root(repo_path.resolve())))
    except GitError as exc:
        console.print(f"dowel: {exc}", style="red")
        raise typer.Exit(2) from exc
    found = history.find(repo, commits)
    if not found:
        console.print(f"No fix in the last {commits} commits looks undone.", style=MUTED)
        return
    console.print(f"{len(found)} fix{'es' if len(found) != 1 else ''} whose lines have since gone. "
                  "Worth a look; a fix can also be removed because something better replaced it.\n", style=MUTED)
    for u in found:
        line = Text(f"{u.fix}  ", style=AMBER)
        line.append(u.fix_subject, style="bold")
        console.print(line)
        console.print(Text(f"    {u.file} · {u.fix_date} · {len(u.gone)} of {u.of} added lines are gone", style=MUTED))
        for text in u.gone[:2]:
            console.print(Text(f"      {text[:110]}", style=MUTED))
        if u.removed_by:
            console.print(Text(f"    taken out by {u.removed_by}"))


@app.command("adopt")
def adopt_cmd(
    repo_path: Annotated[Path, typer.Option("--repo", help="Repository.")] = Path("."),
    take: Annotated[str, typer.Option(help="Numbers to record, such as 1,3,4, or all.")] = "",
    commits: Annotated[int, typer.Option(help="How many recent commits to read.")] = 300,
) -> None:
    """Propose decisions from fixes already in this repository's history. No model, no questions."""
    from dowel import adopt
    from dowel.gitops import repo_root
    from dowel.memory import record_stated

    try:
        repo = Path(asyncio.run(repo_root(repo_path.resolve())))
    except GitError as exc:
        console.print(f"dowel: {exc}", style="red")
        raise typer.Exit(2) from exc
    found = adopt.find(repo, commits)
    if not found:
        console.print("No fix in the recent history is waiting to be recorded.", style=MUTED)
        return
    if take:
        wanted = range(1, len(found) + 1) if take.strip() == "all" else [int(n) for n in take.split(",") if n.strip().isdigit()]
        done = 0
        for n in wanted:
            if 1 <= n <= len(found):
                c = found[n - 1]
                record_stated(repo, c.file, c.line, c.title, "keep", c.reason, "from history")
                done += 1
        console.print(f"Recorded {done} decision{'s' if done != 1 else ''} in .dowel/decisions. Look them over, edit the "
                      "wording if you like, and commit them.", style=MUTED)
        return
    console.print(f"{len(found)} fixes still in the code could be decisions to keep:\n", style=MUTED)
    for n, c in enumerate(found, 1):
        line = Text(f"{n:>2}  ", style=AMBER)
        line.append(c.title, style="bold")
        console.print(line)
        console.print(Text(f"    {c.file} · {c.symbol} · {c.commit} on {c.date}", style=MUTED))
    console.print('\nRecord some with `dowel adopt --take 1,3` or all with `dowel adopt --take all`.', style=MUTED)


AGENTS_NOTE = """## Decisions on record

The developers of this repository record decisions about the code under `.dowel/decisions/`.
Before you edit a file, run `dowel decisions <path>` and read what it prints. If your change would
go against a decision, stop and ask the developer first. Do not edit files under `.dowel/`.
"""


@app.command("hook")
def hook_cmd(
    repo_path: Annotated[Path, typer.Option("--repo", help="Repository.")] = Path("."),
    agents_md: Annotated[bool, typer.Option("--agents-md", help="Also add a short section to AGENTS.md, for "
                                            "agents that have no hooks.")] = False,
) -> None:
    """Set up Claude Code in this repository to see recorded decisions before it edits a file."""
    import shlex
    import sys

    from dowel import hook
    from dowel.gitops import repo_root

    try:
        repo = asyncio.run(repo_root(repo_path.resolve()))
    except GitError as exc:
        console.print(f"dowel: {exc}", style="red")
        raise typer.Exit(1) from exc
    written = hook.install(Path(repo), f"{shlex.quote(sys.executable)} -m dowel.hook")
    if agents_md:
        note = Path(repo) / "AGENTS.md"
        text = note.read_text() if note.exists() else ""
        if "dowel decisions" not in text:
            note.write_text(text.rstrip("\n") + ("\n\n" if text else "") + AGENTS_NOTE)
        console.print("Added a short section to AGENTS.md for agents that have no hooks.", style=MUTED)
    console.print(f"Claude Code will now be shown this repository's decisions when it opens a file.\n"
                  f"Set up in {written.relative_to(repo)}, which stays on this machine.", style=MUTED)


@app.command("decisions")
def decisions_cmd(
    paths: Annotated[list[str], typer.Argument(help="Files about to be edited, relative to the repository.")],
    repo_path: Annotated[Path, typer.Option("--repo", help="Repository.")] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output, for hooks.")] = False,
) -> None:
    """Print the decisions recorded against these files. No model is called.

    For a coding agent to read before it edits. The text is what a developer decided, quoted;
    it is information about the code, not an instruction to follow blindly.
    """
    import json as jsonlib

    from dowel.gitops import repo_root
    from dowel.memory import Memory

    try:
        repo = asyncio.run(repo_root(repo_path.resolve()))
    except GitError as exc:
        console.print(f"dowel: {exc}", style="red")
        raise typer.Exit(1) from exc
    wanted = []
    for raw in paths:
        full = Path(raw) if Path(raw).is_absolute() else (repo_path.resolve() / raw)
        try:
            wanted.append(full.resolve().relative_to(repo.resolve()).as_posix())
        except ValueError:
            continue  # outside this repository: nothing recorded about it here
    memory = Memory(repo)
    try:
        found = [
            {"file": where.file, "symbol": where.symbol, "line": where.first, "stance": d.stance,
             "decision": d.title, "reason": d.reason, "decided": d.day, "code_changed_since": where.state == "drifted"}
            for d, where in memory.located(wanted)
            if where.file in wanted and d.stance in ("keep", "change") and not d.suspect
        ]
    finally:
        memory.close()
    if as_json:
        print(jsonlib.dumps(found, indent=2))
        return
    if not found:
        return  # silence is the answer when nothing was decided about these files
    print("Decisions the developers of this repository recorded about this code:")
    for item in found:
        verdict = "keep this as it is" if item["stance"] == "keep" else "this should be changed"
        why = f' Their reason: "{item["reason"]}"' if item["reason"] else ""
        drift = " (the code has changed since)" if item["code_changed_since"] else ""
        print(f'- {item["file"]}:{item["line"]} {item["symbol"] or "file"}: "{item["decision"]}" '
              f'Decided on {item["decided"]}: {verdict}.{why}{drift}')
    print("If your change would go against one of these, stop and ask the developer first.")


@app.command("memory")
def memory_cmd(
    path: Annotated[Path, typer.Option("--repo", help="Repository.")] = Path("."),
    forget: Annotated[str | None, typer.Option(help="Retire one record, such as decision:3 or noise:1.")] = None,
    because: Annotated[str, typer.Option(help="Why a decision is being retired. Kept with it.")] = "",
    export: Annotated[bool, typer.Option("--export", help="Write decisions kept only on this machine as files "
                                         "in the repository.")] = False,
) -> None:
    """Show the decisions recorded for this repository, or retire one."""
    from dowel.gitops import repo_root
    from dowel.memory import Memory

    try:
        repo = asyncio.run(repo_root(path.resolve()))
    except GitError as exc:
        console.print(f"dowel: {exc}", style="red")
        raise typer.Exit(1) from exc
    memory = Memory(repo)
    try:
        if forget:
            kind, _, number = forget.partition(":")
            done = number.isdigit() and memory.forget(kind, int(number), because)
            console.print("Retired." if done else "No such record. Use decision:<number> or noise:<number>.",
                          style=MUTED if done else "red")
            return
        if export:
            count = memory.export_local()
            console.print(f"Wrote {count} decision{'s' if count != 1 else ''} to .dowel/decisions. "
                          "Nothing is committed: look them over, then commit them yourself.", style=MUTED)
            return
        located, noise = memory.located(), memory.noise_rules()
        if not located and not noise:
            console.print("Nothing remembered for this repository yet.", style=MUTED)
            return
        if located:
            console.print("decisions", style=MUTED)
            for d, where in located:
                line = Text(f"  decision:{d.id}".ljust(15), style=AMBER)
                line.append(f"{d.rule}")
                console.print(line)
                if where.state == "orphaned":
                    place = f"{d.file} · {d.symbol or 'whole file'} · code not found"
                else:
                    moved = f" ({where.how} from {d.file} · {d.symbol})" if where.how else ""
                    changed = " · code changed since" if where.state == "drifted" else ""
                    place = f"{where.file} · {where.symbol or 'whole file'}{moved}{changed}"
                kept = "" if d.uid else " · only on this machine"
                held = " · held back from agents: reads like an instruction" if d.suspect else ""
                console.print(Text(f"               {place} · {d.day}{kept}{held}", style=MUTED))
            for a, b in memory.conflicts():
                console.print(Text(f"\n  decision:{a.id} and decision:{b.id} disagree about the same code. "
                                   "Retire one.", style=AMBER))
        if noise:
            console.print("\nnot raised any more", style=MUTED)
            for rule in noise:
                line = Text(f"  noise:{rule['id']}".ljust(15), style=AMBER)
                line.append(rule["example"])
                console.print(line)
                console.print(Text(f"               {rule['scope']} · held back {rule['hits']} times", style=MUTED))
        lost = sum(where.state == "orphaned" for _, where in located)
        if lost:
            console.print(f"\n{lost} could not be matched to any code. Forget them, or they stay listed.", style=MUTED)
    finally:
        memory.close()
