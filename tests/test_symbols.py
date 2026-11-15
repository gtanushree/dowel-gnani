"""The code map names what a change touches in each supported language."""

import pytest

from dowel.symbols import LANGUAGES, touched

CASES = [
    ("svc/loader.py", "class Loader:\n    def load(self, path):\n        return 1\n", [(3, 3)], ["load", "Loader"]),
    ("svc/server.go", "package a\n\nfunc (s *Server) Handle(w int) error {\n\treturn nil\n}\n\ntype Config struct {\n\tA int\n}\n", [(4, 4), (8, 8)], ["Handle", "Config"]),
    ("web/api.ts", "export interface User { id: string }\n\nexport const fetchUser = async (id: string) => {\n  return id;\n};\n\nclass Store {\n  save(u: User) {\n    return u;\n  }\n}\n", [(4, 4), (9, 9)], ["fetchUser", "save", "Store"]),
    ("web/App.tsx", "export function App() {\n  return <div>hi</div>;\n}\n", [(2, 2)], ["App"]),
    ("lib/util.js", "function parse(x) {\n  return x;\n}\nconst limit = 3;\n", [(2, 2), (4, 4)], ["parse"]),
    ("src/lib.rs", "pub struct Queue { n: u32 }\n\nimpl Queue {\n    pub fn push(&mut self) {\n        self.n += 1;\n    }\n}\n", [(5, 5)], ["push", "Queue"]),
    ("src/parse.c", "static int *parse(const char *s) {\n    return 0;\n}\n", [(2, 2)], ["parse"]),
    ("src/queue.cpp", "namespace q {\nclass Queue {\n public:\n  void Push(int v) {\n    n_ += v;\n  }\n  int n_;\n};\n}\n", [(5, 5)], ["Push", "Queue", "q"]),
    ("App.java", "class App {\n  int run(int a) {\n    return a;\n  }\n}\n", [(3, 3)], ["run", "App"]),
    ("app.rb", "class App\n  def run\n    1\n  end\nend\n", [(3, 3)], ["run", "App"]),
]


@pytest.mark.parametrize(("path", "source", "ranges", "expected"), CASES, ids=[c[0] for c in CASES])
def test_touched_definitions(path: str, source: str, ranges: list[tuple[int, int]], expected: list[str]) -> None:
    assert touched(path, source, ranges) == expected


def test_unknown_language_falls_back() -> None:
    assert touched("notes.xyz", "anything", [(1, 1)]) is None


def test_every_listed_language_has_a_parser() -> None:
    for extension, grammar in LANGUAGES.items():
        assert touched(f"file{extension}", "", [(1, 1)]) is not None, grammar
