"""New dependencies that do not exist or are days old."""

from dowel import packages

DIFF = """--- a/requirements.txt
+++ b/requirements.txt
@@ -1,2 +1,4 @@
 httpx==0.27
+requests>=2.31
+fastjsonvalidatorx==1.0
--- a/web/package.json
+++ b/web/package.json
@@ -5,3 +5,5 @@
     "react": "^19.0.0",
+    "left-pad-ultra": "^1.0.0",
+    "@scope/real-thing": "~2.1.0",
-    "old": "1.0.0"
+    "name": "my-app",
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -9,2 +9,3 @@
+    "pydantic>=2",
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
+requests = 3
"""


def test_only_real_dependency_lines_are_picked_up() -> None:
    assert packages.added(DIFF) == [
        ("pypi", "requests", "requirements.txt"), ("pypi", "fastjsonvalidatorx", "requirements.txt"),
        ("npm", "left-pad-ultra", "web/package.json"), ("npm", "@scope/real-thing", "web/package.json"),
        ("pypi", "pydantic", "pyproject.toml"),
    ]


def test_missing_and_brand_new_packages_are_flagged_and_an_unreachable_registry_is_not() -> None:
    now = 1_800_000_000.0
    known = {"requests": (True, now - 4000 * 86400), "fastjsonvalidatorx": (False, None),
             "left-pad-ultra": (True, now - 3 * 86400), "@scope/real-thing": None, "pydantic": (True, None)}
    flagged, notes = packages.check(DIFF, lambda _registry, name: known[name], now)
    assert [(f.name, f.problem) for f in flagged] == [
        ("fastjsonvalidatorx", "no package called fastjsonvalidatorx exists on pypi"),
        ("left-pad-ultra", "left-pad-ultra was first published 3 days ago"),
    ]
    assert notes == ["could not reach npm to look up @scope/real-thing"]
