"""Which functions, classes and types a change touches, for every language Dowel supports.

Each file is parsed with tree-sitter and the definitions whose lines overlap the change are
returned by name. The same code path serves all languages; adding one means adding its file
extensions and the node types that count as definitions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import PurePosixPath
from typing import Any

# extension -> tree-sitter grammar
LANGUAGES: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".go": "go",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".rs": "rust",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
}

_JS = {"function_declaration", "generator_function_declaration", "class_declaration", "method_definition",
       "variable_declarator", "interface_declaration", "type_alias_declaration", "enum_declaration",
       "abstract_class_declaration"}

# grammar -> node types that define something worth naming
DEFINITIONS: dict[str, set[str]] = {
    "python": {"function_definition", "class_definition"},
    "go": {"function_declaration", "method_declaration", "type_spec"},
    "typescript": _JS, "tsx": _JS, "javascript": _JS,
    "rust": {"function_item", "struct_item", "enum_item", "trait_item", "impl_item", "mod_item", "const_item",
             "static_item", "type_item", "macro_definition"},
    "c": {"function_definition", "struct_specifier", "enum_specifier", "type_definition"},
    "cpp": {"function_definition", "struct_specifier", "class_specifier", "enum_specifier", "namespace_definition"},
    "java": {"method_declaration", "constructor_declaration", "class_declaration", "interface_declaration",
             "enum_declaration", "record_declaration"},
    "kotlin": {"function_declaration", "class_declaration", "object_declaration"},
    "ruby": {"method", "singleton_method", "class", "module"},
    "php": {"function_definition", "method_declaration", "class_declaration", "interface_declaration",
            "trait_declaration"},
    "csharp": {"method_declaration", "constructor_declaration", "class_declaration", "interface_declaration",
               "struct_declaration", "record_declaration", "enum_declaration"},
    "swift": {"function_declaration", "class_declaration", "protocol_declaration"},
}

_NAME_NODES = {"identifier", "type_identifier", "field_identifier", "simple_identifier", "property_identifier",
               "constant", "name"}


def language_of(path: str) -> str | None:
    return LANGUAGES.get(PurePosixPath(path).suffix.lower())


@cache
def _parser(grammar: str) -> Any:
    from tree_sitter_language_pack import get_parser

    return get_parser(grammar)


def _text(node: Any) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _name(node: Any) -> str:
    named = node.child_by_field_name("name")
    if named is not None:
        return _text(named)
    if node.type == "impl_item":  # Rust: `impl Trait for Type` is named by its type
        return _text(node.child_by_field_name("type"))
    # C and C++ put the name inside nested declarators: `int *parse(const char *s)`.
    inner = node.child_by_field_name("declarator")
    for _ in range(6):
        if inner is None:
            break
        if inner.type in _NAME_NODES or inner.type in ("qualified_identifier", "destructor_name", "operator_name"):
            return _text(inner).split("::")[-1]
        inner = inner.child_by_field_name("declarator") or inner.child_by_field_name("name")
    for child in node.children:  # grammars without a name field (Kotlin)
        if child.type in _NAME_NODES:
            return _text(child)
    return ""


def touched(path: str, source: str, ranges: list[tuple[int, int]]) -> list[str] | None:
    """Names of the definitions overlapping the changed line ranges, innermost first.

    Returns None when the language is not supported, so the caller can fall back.
    """
    grammar = language_of(path)
    if grammar is None:
        return None
    try:
        tree = _parser(grammar).parse(source.encode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - an unparseable file must not stop a review
        return None
    kinds = DEFINITIONS[grammar]
    found: list[tuple[int, int, str]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        first, last = node.start_point[0] + 1, node.end_point[0] + 1
        if not any(first <= hi and last >= lo for lo, hi in ranges):
            continue  # nothing below this node was changed
        if node.type in kinds:
            if node.type == "variable_declarator":
                value = node.child_by_field_name("value")
                if value is None or value.type not in ("arrow_function", "function_expression", "function"):
                    stack.extend(node.children)
                    continue
            name = _name(node)
            if name:
                found.append((last - first, first, name))
        stack.extend(node.children)
    found.sort()  # smallest span first: the function before the class that holds it
    return list(dict.fromkeys(name for _, _, name in found))


@dataclass(frozen=True)
class Definition:
    """One named thing in a file, as decisions need it."""

    name: str  # qualified: `Loader.load_config`, not `load_config`
    first: int
    last: int
    fingerprint: str  # of its code tokens, so comments and layout do not count as a change

    @property
    def short(self) -> str:
        return self.name.rsplit(".", 1)[-1]


def _fingerprint(node: Any) -> str:
    """A hash of the tokens that make up a definition, ignoring comments, whitespace and its own name.

    Leaving the name out is what lets a decision follow a function that was only renamed.
    """
    own_name = node.child_by_field_name("name")
    tokens: list[bytes] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if "comment" in current.type or (own_name is not None and current.id == own_name.id):
            continue
        if current.child_count == 0:
            tokens.append(current.text or b"")
        else:
            stack.extend(reversed(current.children))
    return hashlib.sha256(b"\x00".join(tokens)).hexdigest()[:16]


def definitions(path: str, source: str) -> list[Definition]:
    """Every definition in a file, outermost first, each named by the definitions that contain it."""
    grammar = language_of(path)
    if grammar is None:
        return []
    try:
        tree = _parser(grammar).parse(source.encode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - an unparseable file has no definitions to anchor to  # noqa: BLE001
        return []
    kinds = DEFINITIONS[grammar]
    out: list[Definition] = []
    stack: list[tuple[Any, str]] = [(tree.root_node, "")]
    while stack:
        node, outer = stack.pop()
        inner = outer
        if node.type in kinds and not _is_plain_variable(node):
            name = _owner(node) + _name(node) if _name(node) else ""
            if name:
                inner = f"{outer}.{name}" if outer else name
                out.append(Definition(inner, node.start_point[0] + 1, node.end_point[0] + 1, _fingerprint(node)))
        stack.extend((child, inner) for child in reversed(node.children))
    return out


def _owner(node: Any) -> str:
    """The type a definition belongs to when the language writes it outside that type's body."""
    if node.type == "method_declaration":  # Go: `func (s *Server) Run()`
        receiver = node.child_by_field_name("receiver")
        if receiver is not None:
            names = [_text(n) for n in _walk(receiver) if n.type == "type_identifier"]
            return f"{names[0]}." if names else ""
    if node.type == "function_definition":  # C++: `void Server::run()`
        inner = node.child_by_field_name("declarator")
        for _ in range(6):
            if inner is None:
                break
            if inner.type == "qualified_identifier":
                return "".join(f"{part}." for part in _text(inner).split("::")[:-1])
            inner = inner.child_by_field_name("declarator")
    return ""


def _walk(node: Any) -> list[Any]:
    out, stack = [], [node]
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(reversed(current.children))
    return out


def _is_plain_variable(node: Any) -> bool:
    """`const x = 3` is not a definition; `const f = () => ...` is."""
    if node.type != "variable_declarator":
        return False
    value = node.child_by_field_name("value")
    return value is None or value.type not in ("arrow_function", "function_expression", "function")


def enclosing(path: str, source: str, line: int) -> Definition | None:
    """The smallest definition containing a line."""
    inside = [d for d in definitions(path, source) if d.first <= line <= d.last]
    return min(inside, key=lambda d: d.last - d.first) if inside else None


def find(path: str, source: str, name: str) -> Definition | None:
    """A definition by its qualified name. A bare name is accepted when only one definition has it."""
    found = definitions(path, source)
    exact = [d for d in found if d.name == name]
    if exact:
        return exact[0]
    if "." not in name:
        loose = [d for d in found if d.short == name]
        if len(loose) == 1:
            return loose[0]
    return None


def find_by_fingerprint(path: str, source: str, fingerprint: str) -> Definition | None:
    """The definition whose code is exactly this, wherever it now sits and whatever it is now called."""
    same = [d for d in definitions(path, source) if d.fingerprint == fingerprint]
    return same[0] if len(same) == 1 else None  # two identical bodies: no way to say which one it was
