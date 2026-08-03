"""tree-sitter first pass over Python sources.

Error-tolerant candidate scan: locates decorated functions/methods and class
definitions with exact line spans even in files with syntax errors, so route
discovery never depends on the whole file parsing cleanly. Precise extraction
is done later with libcst/astroid on the shortlisted files only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from openapi_agent.logging_utils import get_logger

log = get_logger("analysis.python.ts")


@dataclass(frozen=True)
class TsDecoratedDef:
    name: str
    kind: str  # "function" | "class"
    class_name: str | None  # enclosing class for methods
    decorators: tuple[str, ...]  # decorator source text, without leading '@'
    start_line: int
    end_line: int


@dataclass(frozen=True)
class TsClassDef:
    name: str
    bases: tuple[str, ...]
    decorators: tuple[str, ...]
    start_line: int
    end_line: int


@dataclass
class PyFileIndex:
    path: str  # repo-relative POSIX
    parse_ok: bool = True
    has_errors: bool = False
    decorated: list[TsDecoratedDef] = field(default_factory=list)
    classes: list[TsClassDef] = field(default_factory=list)


@lru_cache(maxsize=1)
def _python_language():
    import tree_sitter_python
    from tree_sitter import Language

    return Language(tree_sitter_python.language())


def _make_parser():
    from tree_sitter import Parser

    return Parser(_python_language())


def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def scan_python_file(abs_path: Path, rel_path: str) -> PyFileIndex:
    index = PyFileIndex(path=rel_path)
    try:
        source = abs_path.read_bytes()
    except OSError as exc:
        log.debug("unreadable %s: %s", rel_path, exc)
        index.parse_ok = False
        return index

    parser = _make_parser()
    tree = parser.parse(source)
    root = tree.root_node
    index.has_errors = root.has_error

    def walk(node, enclosing_class: str | None) -> None:
        for child in node.children:
            node_type = child.type
            if node_type == "decorated_definition":
                decorators = tuple(
                    _text(d, source).lstrip("@").strip()
                    for d in child.children
                    if d.type == "decorator"
                )
                definition = child.child_by_field_name("definition")
                if definition is not None and definition.type in (
                    "function_definition",
                    "async_function_definition",
                ):
                    name_node = definition.child_by_field_name("name")
                    if name_node is not None:
                        index.decorated.append(
                            TsDecoratedDef(
                                name=_text(name_node, source),
                                kind="function",
                                class_name=enclosing_class,
                                decorators=decorators,
                                start_line=child.start_point[0] + 1,
                                end_line=definition.end_point[0] + 1,
                            )
                        )
                    walk(definition, enclosing_class)
                elif definition is not None and definition.type == "class_definition":
                    _record_class(definition, decorators, enclosing_class)
            elif node_type == "class_definition":
                _record_class(child, (), enclosing_class)
            elif node_type in ("function_definition", "async_function_definition", "block", "module", "if_statement", "try_statement"):
                walk(child, enclosing_class)

    def _record_class(definition, decorators: tuple[str, ...], enclosing_class: str | None) -> None:
        name_node = definition.child_by_field_name("name")
        name = _text(name_node, source) if name_node is not None else "<anonymous>"
        superclasses = definition.child_by_field_name("superclasses")
        bases: tuple[str, ...] = ()
        if superclasses is not None:
            bases = tuple(
                _text(b, source)
                for b in superclasses.children
                if b.type not in ("(", ")", ",", "comment", "keyword_argument")
            )
        index.classes.append(
            TsClassDef(
                name=name,
                bases=bases,
                decorators=decorators,
                start_line=definition.start_point[0] + 1,
                end_line=definition.end_point[0] + 1,
            )
        )
        if decorators:
            index.decorated.append(
                TsDecoratedDef(
                    name=name,
                    kind="class",
                    class_name=enclosing_class,
                    decorators=decorators,
                    start_line=definition.start_point[0] + 1,
                    end_line=definition.end_point[0] + 1,
                )
            )
        body = definition.child_by_field_name("body")
        if body is not None:
            walk(body, name)

    walk(root, None)
    return index


def scan_repository(repo_root: Path, python_files: list[str]) -> dict[str, PyFileIndex]:
    """Index every python file; never raises on individual file failures."""
    indexes: dict[str, PyFileIndex] = {}
    for rel in python_files:
        indexes[rel] = scan_python_file(repo_root / rel, rel)
    errored = sum(1 for i in indexes.values() if i.has_errors or not i.parse_ok)
    if errored:
        log.info("tree-sitter scan: %d/%d files with syntax issues", errored, len(indexes))
    return indexes
