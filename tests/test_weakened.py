"""Finding tests a change made weaker, from the diff alone."""

import subprocess
from pathlib import Path

import pytest

from dowel import weakened

TEST = '''import pytest
from loader import load


def test_missing_file_falls_back():
    config = load(None)
    assert config.retries == 3
    assert config.signed is False


def test_bad_value_is_refused():
    with pytest.raises(ValueError):
        load({"retries": -1})
'''


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_loader.py").write_text(TEST)
    (tmp_path / "loader.py").write_text("def load(x):\n    return x\n")
    git(tmp_path, "add", "."), git(tmp_path, "commit", "-qm", "start")
    return tmp_path


def found(repo: Path) -> list[tuple[str, str]]:
    git(repo, "add", "-A")
    diff = git(repo, "diff", "--cached", "-M", "--unified=0", "--no-color")
    status = git(repo, "diff", "--cached", "-M", "--name-status")
    return [(w.kind, w.detail) for w in weakened.find(diff, status)]


def change(repo: Path, old: str, new: str) -> None:
    path = repo / "tests/test_loader.py"
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, new))


def test_an_honest_change_to_a_test_is_not_flagged(repo: Path) -> None:
    change(repo, "assert config.retries == 3", "assert config.retries == 5")
    (repo / "loader.py").write_text("def load(x):\n    return x or {}\n")
    assert found(repo) == []


def test_removed_assertions_are_counted(repo: Path) -> None:
    change(repo, "    assert config.signed is False\n", "")
    assert found(repo) == [("assertions removed", "1 fewer assertion than before")]


def test_an_assertion_swapped_for_one_that_cannot_fail(repo: Path) -> None:
    change(repo, "assert config.retries == 3", "assert True")
    kinds = [k for k, _ in found(repo)]
    assert kinds == ["assertions removed", "trivial assertion"]


def test_a_test_marked_skipped(repo: Path) -> None:
    change(repo, "def test_bad_value_is_refused", '@pytest.mark.skip(reason="flaky")\ndef test_bad_value_is_refused')
    assert found(repo) == [("skipped", "1 test marked to be skipped")]


def test_a_removed_test_is_named_once(repo: Path) -> None:
    change(repo, TEST[TEST.index("def test_bad_value"):], "")
    assert found(repo) == [("tests removed", "1 test removed: test_bad_value_is_refused")]


def test_a_deleted_test_file_and_one_renamed_out_of_the_runners_sight(repo: Path) -> None:
    (repo / "tests/test_other.py").write_text("def test_other():\n    assert 1 + 1 == 2\n")
    git(repo, "add", "."), git(repo, "commit", "-qm", "more")
    (repo / "tests/test_other.py").unlink()
    (repo / "tests/test_loader.py").rename(repo / "tests/test_loader.py.disabled")
    assert found(repo) == [("disabled", "renamed to tests/test_loader.py.disabled, which the test runner will not pick up"),
                           ("deleted", "the test file was deleted")]


def test_other_languages() -> None:
    diff = """--- a/src/pay.test.ts
+++ b/src/pay.test.ts
@@ -3,2 +3,2 @@
-  expect(total).toBe(42);
+  expect(true).toBe(true);
@@ -9 +9 @@
-  it('charges once', () => {
+  it.skip('charges once', () => {
--- a/pay_test.go
+++ b/pay_test.go
@@ -5,3 +5,1 @@
-	if got != 42 { t.Fatalf("got %d", got) }
-	require.NoError(t, err)
+	t.Skip("later")
"""
    kinds = {(w.file, w.kind) for w in weakened.find(diff, "M\tsrc/pay.test.ts\nM\tpay_test.go")}
    assert kinds == {("src/pay.test.ts", "assertions removed"), ("src/pay.test.ts", "trivial assertion"),
                     ("src/pay.test.ts", "skipped"), ("pay_test.go", "assertions removed"), ("pay_test.go", "skipped")}


def test_a_changed_guard_test_is_reported_whatever_the_change(repo: Path) -> None:
    (repo / ".dowel/guards").mkdir(parents=True)
    (repo / ".dowel/guards/test_signing.py").write_text("def test_signing_stays_on():\n    assert True is not False\n")
    git(repo, "add", "."), git(repo, "commit", "-qm", "guard")
    (repo / ".dowel/guards/test_signing.py").write_text("def test_signing_stays_on():\n    assert 2 > 1\n")
    assert found(repo) == [("guard edited", "a guard test written from a decision was changed")]


def test_a_renamed_test_and_a_new_skipped_test_are_not_weakenings(repo: Path) -> None:
    change(repo, "def test_bad_value_is_refused", "def test_negative_retries_are_refused")
    assert found(repo) == []
    change(repo, "import pytest\n", "import pytest\nimport sys\n")
    path = repo / "tests/test_loader.py"
    path.write_text(path.read_text() + '\n\n@pytest.mark.skipif(sys.platform == "win32", reason="posix only")\n'
                    "def test_file_modes():\n    assert load(None) is None\n")
    assert found(repo) == []


def test_a_newly_written_guard_test_is_not_an_edited_one(repo: Path) -> None:
    (repo / ".dowel/guards").mkdir(parents=True)
    (repo / ".dowel/guards/test_signing.py").write_text("def test_signing_stays_on():\n    assert 2 > 1\n")
    assert found(repo) == []
