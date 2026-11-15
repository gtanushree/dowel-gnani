"""Splitting a change into parts: what is reviewed, what is set aside, and that nothing vanishes."""

from dowel.plan import make_plan, split_diff


def _file(path: str, added: int, binary: bool = False) -> str:
    if binary:
        return f"diff --git a/{path} b/{path}\nBinary files a/{path} and b/{path} differ\n"
    body = "\n".join(f"+line {n}" for n in range(added))
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{added} @@\n{body}\n"


def test_split_counts_added_and_removed_lines() -> None:
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-old\n+new\n keep\n"
    (only,) = split_diff(diff)
    assert (only.path, only.added, only.removed) == ("a.py", 1, 1)


def test_lock_files_and_binaries_are_set_aside_but_docs_are_reviewed() -> None:
    diff = _file("src/app.py", 5) + _file("uv.lock", 900) + _file("README.md", 40) + _file("logo.png", 0, binary=True)
    plan = make_plan(diff, part_chars=90_000, max_parts=16)
    assert [f.path for part in plan.parts for f in part.files] == ["README.md", "src/app.py"]
    assert sorted(line.split(":")[0] for line in plan.skipped) == ["logo.png", "uv.lock"]


def test_a_large_change_is_split_and_every_file_is_accounted_for() -> None:
    paths = [f"pkg/mod{n}.py" for n in range(12)]
    plan = make_plan("".join(_file(p, 300) for p in paths), part_chars=10_000, max_parts=3)
    reviewed = {f.path for part in plan.parts for f in part.files}
    left = {line.split(":")[0] for line in plan.skipped}
    assert len(plan.parts) == 3
    assert reviewed | left == set(paths) and not reviewed & left
    assert all("beyond what one run reads" in line for line in plan.skipped)
