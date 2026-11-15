"""The command line loads, lists every command, and the ones that need no model run."""

from pathlib import Path

from typer.testing import CliRunner

from dowel.cli import app

run = CliRunner()


def test_every_command_is_listed() -> None:
    out = run.invoke(app, ["--help"]).output
    for name in ("show", "open", "talk", "check", "brief", "guard", "mcp", "history", "adopt", "hook", "decisions", "memory"):
        assert f" {name} " in out, name


def test_the_sample_review_prints_with_no_keys_and_no_repository(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    done = run.invoke(app, ["show", "--sample"])
    assert done.exit_code == 0 and "falls back to defaults" in done.output and "keep" in done.output


def test_commands_outside_a_repository_say_so_instead_of_crashing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    for command in (["check", "--offline"], ["brief"], ["history"], ["adopt"], ["memory"]):
        done = run.invoke(app, [*command, "--repo", str(tmp_path)])
        assert done.exit_code in (1, 2) and "not inside a git repository" in done.output, command
