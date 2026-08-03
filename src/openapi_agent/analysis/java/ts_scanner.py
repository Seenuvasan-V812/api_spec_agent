"""tree-sitter-java repository scan + lightweight symbol index.

This is the always-available Java front end (no JVM required). It extracts
classes, annotations (with parsed arguments), fields, methods, parameters,
throw sites, and enum constants, and provides simple-name → qualified-name
resolution via package/import tracking. The JavaParser sidecar (when built and
a JVM is present) *augments* these facts with solver-grade type resolution;
its absence degrades confidence, never correctness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from openapi_agent.logging_utils import get_logger

log = get_logger("analysis.java.ts")

_JAVA_LANG_TYPES = {
    "String", "Integer", "Long", "Short", "Byte", "Double", "Float", "Boolean",
    "Character", "Object", "Void", "Number", "Iterable", "Exception", "RuntimeException",
}


@dataclass(frozen=True)
class JavaAnnotation:
    name: str  # simple name, e.g. "GetMapping"
    line: int
    value: str | None = None  # single-element value (parsed string/text)
    kwargs: tuple[tuple[str, str], ...] = ()  # element name -> raw text (quotes stripped)

    def kw(self, key: str, default: str | None = None) -> str | None:
        for k, v in self.kwargs:
            if k == key:
                return v
        return default

    def kw_list(self, key: str) -> list[str]:
        raw = self.kw(key)
        if raw is None and key == "value" and self.value is not None:
            raw = self.value
        if raw is None:
            return []
        raw = raw.strip()
        if raw.startswith("{") and raw.endswith("}"):
            raw = raw[1:-1]
        return [part.strip().strip('"') for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class JavaParam:
    name: str
    type_text: str
    annotations: tuple[JavaAnnotation, ...] = ()


@dataclass(frozen=True)
class JavaThrowSite:
    exception_type: str  # simple or qualified text
    line: int
    arg_text: str | None = None


@dataclass
class JavaMethod:
    name: str
    return_type: str
    params: list[JavaParam] = field(default_factory=list)
    annotations: list[JavaAnnotation] = field(default_factory=list)
    throws_decl: list[str] = field(default_factory=list)
    throw_sites: list[JavaThrowSite] = field(default_factory=list)
    invocations: list[str] = field(default_factory=list)  # called method names (for call chain)
    body_text: str = ""  # used by the WebFlux functional-route parser
    start_line: int = 1
    end_line: int = 1
    javadoc: str | None = None
    modifiers: tuple[str, ...] = ()


@dataclass
class JavaField:
    name: str
    type_text: str
    annotations: list[JavaAnnotation] = field(default_factory=list)
    javadoc: str | None = None
    line: int = 1
    modifiers: tuple[str, ...] = ()


@dataclass
class JavaClass:
    package: str
    name: str
    kind: str  # class | interface | enum | record | annotation
    file: str  # repo-relative POSIX
    start_line: int = 1
    end_line: int = 1
    annotations: list[JavaAnnotation] = field(default_factory=list)
    extends_text: str | None = None
    implements: list[str] = field(default_factory=list)
    type_params: list[str] = field(default_factory=list)
    fields: list[JavaField] = field(default_factory=list)
    methods: list[JavaMethod] = field(default_factory=list)
    enum_constants: list[str] = field(default_factory=list)
    record_components: list[JavaParam] = field(default_factory=list)
    javadoc: str | None = None
    imports: list[str] = field(default_factory=list)  # copied from file scope
    wildcard_imports: list[str] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.package}.{self.name}" if self.package else self.name

    def find_annotation(self, *names: str) -> JavaAnnotation | None:
        for annotation in self.annotations:
            if annotation.name in names:
                return annotation
        return None


@dataclass
class JavaIndex:
    classes: dict[str, JavaClass] = field(default_factory=dict)  # qualified -> class
    by_simple: dict[str, list[str]] = field(default_factory=dict)  # simple -> [qualified]
    files_failed: list[str] = field(default_factory=list)

    def add(self, cls: JavaClass) -> None:
        self.classes[cls.qualified] = cls
        self.by_simple.setdefault(cls.name, []).append(cls.qualified)

    def resolve(self, type_name: str, from_class: JavaClass | None = None) -> JavaClass | None:
        """Resolve a (possibly generic/qualified) type text to an indexed class."""
        base = type_name.split("<", 1)[0].strip().rstrip("[]").strip()
        if base in self.classes:
            return self.classes[base]
        if from_class is not None:
            for imported in from_class.imports:
                if imported.rsplit(".", 1)[-1] == base and imported in self.classes:
                    return self.classes[imported]
            same_package = f"{from_class.package}.{base}" if from_class.package else base
            if same_package in self.classes:
                return self.classes[same_package]
            for wildcard_package in from_class.wildcard_imports:
                candidate = f"{wildcard_package}.{base}"
                if candidate in self.classes:
                    return self.classes[candidate]
            # nested class of the same file/class
            nested = f"{from_class.qualified}.{base}"
            if nested in self.classes:
                return self.classes[nested]
        candidates = self.by_simple.get(base, [])
        if len(candidates) == 1:
            return self.classes[candidates[0]]
        return None


@lru_cache(maxsize=1)
def _java_language():
    import tree_sitter_java
    from tree_sitter import Language

    return Language(tree_sitter_java.language())


def _parser():
    from tree_sitter import Parser

    return Parser(_java_language())


def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


_JAVA_ESCAPES = {"\\\\": "\\", '\\"': '"', "\\'": "'", "\\n": "\n", "\\t": "\t", "\\r": "\r"}


def _strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        inner = text[1:-1]
        # decode Java string-literal escapes so "\\d" reaches JSON as "\d"
        result = []
        i = 0
        while i < len(inner):
            pair = inner[i : i + 2]
            if pair in _JAVA_ESCAPES:
                result.append(_JAVA_ESCAPES[pair])
                i += 2
            else:
                result.append(inner[i])
                i += 1
        return "".join(result)
    return text


def _parse_annotation(node, source: bytes) -> JavaAnnotation:
    name_node = node.child_by_field_name("name")
    name = _text(name_node, source).rsplit(".", 1)[-1] if name_node is not None else ""
    line = node.start_point[0] + 1
    value: str | None = None
    kwargs: list[tuple[str, str]] = []
    args = node.child_by_field_name("arguments")
    if args is not None:
        for child in args.children:
            if child.type == "element_value_pair":
                key_node = child.child_by_field_name("key")
                value_node = child.child_by_field_name("value")
                if key_node is not None and value_node is not None:
                    kwargs.append((_text(key_node, source), _strip_quotes(_text(value_node, source))))
            elif child.type not in ("(", ")", ","):
                value = _strip_quotes(_text(child, source))
    return JavaAnnotation(name=name, line=line, value=value, kwargs=tuple(kwargs))


def _collect_annotations_and_modifiers(node, source: bytes) -> tuple[list[JavaAnnotation], tuple[str, ...]]:
    annotations: list[JavaAnnotation] = []
    modifiers: list[str] = []
    for child in node.children:
        if child.type == "modifiers":
            for sub in child.children:
                if sub.type in ("annotation", "marker_annotation"):
                    annotations.append(_parse_annotation(sub, source))
                else:
                    modifiers.append(_text(sub, source))
    return annotations, tuple(modifiers)


def _javadoc_before(node, source: bytes) -> str | None:
    sibling = node.prev_named_sibling
    if sibling is not None and sibling.type in ("block_comment", "comment"):
        text = _text(sibling, source)
        if text.startswith("/**"):
            lines = [
                line.strip().lstrip("*").strip()
                for line in text.strip("/*").splitlines()
            ]
            cleaned = [line for line in lines if line and not line.startswith("@")]
            return " ".join(cleaned)[:500] or None
    return None


def _parse_formal_params(params_node, source: bytes) -> list[JavaParam]:
    params: list[JavaParam] = []
    for param in params_node.children:
        if param.type not in ("formal_parameter", "spread_parameter", "record_component"):
            continue
        type_node = param.child_by_field_name("type")
        name_node = param.child_by_field_name("name")
        annotations: list[JavaAnnotation] = []
        for child in param.children:
            if child.type == "modifiers":
                for sub in child.children:
                    if sub.type in ("annotation", "marker_annotation"):
                        annotations.append(_parse_annotation(sub, source))
            elif child.type in ("annotation", "marker_annotation"):
                annotations.append(_parse_annotation(child, source))
        if name_node is not None:
            params.append(
                JavaParam(
                    name=_text(name_node, source),
                    type_text=_text(type_node, source) if type_node is not None else "Object",
                    annotations=tuple(annotations),
                )
            )
    return params


def _scan_method_body(body_node, source: bytes, method: JavaMethod) -> None:
    stack = [body_node]
    while stack:
        node = stack.pop()
        if node.type == "throw_statement":
            for child in node.children:
                if child.type == "object_creation_expression":
                    type_node = child.child_by_field_name("type")
                    args_node = child.child_by_field_name("arguments")
                    if type_node is not None:
                        method.throw_sites.append(
                            JavaThrowSite(
                                exception_type=_text(type_node, source),
                                line=node.start_point[0] + 1,
                                arg_text=_text(args_node, source) if args_node is not None else None,
                            )
                        )
        elif node.type == "method_invocation":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                method.invocations.append(_text(name_node, source))
        stack.extend(node.children)


def _parse_class(node, source: bytes, package: str, rel_path: str, imports: list[str], wildcards: list[str], index: JavaIndex, outer: str | None = None) -> None:
    kind_by_type = {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "record",
        "annotation_type_declaration": "annotation",
    }
    kind = kind_by_type.get(node.type)
    if kind is None:
        return
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    simple_name = _text(name_node, source)
    class_name = f"{outer}.{simple_name}" if outer else simple_name
    annotations, _mods = _collect_annotations_and_modifiers(node, source)
    cls = JavaClass(
        package=package,
        name=class_name,
        kind=kind,
        file=rel_path,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        annotations=annotations,
        javadoc=_javadoc_before(node, source),
        imports=imports,
        wildcard_imports=wildcards,
    )

    superclass = node.child_by_field_name("superclass")
    if superclass is not None:
        cls.extends_text = _text(superclass, source).replace("extends", "", 1).strip()
    interfaces = node.child_by_field_name("interfaces")
    if interfaces is not None:
        text = _text(interfaces, source).replace("implements", "", 1).strip()
        cls.implements = [part.strip() for part in _split_generic_aware(text)]
    type_params = node.child_by_field_name("type_parameters")
    if type_params is not None:
        cls.type_params = [
            _text(c.child_by_field_name("name") or c, source)
            for c in type_params.children
            if c.type == "type_parameter"
        ]
    if kind == "record":
        params_node = node.child_by_field_name("parameters")
        if params_node is not None:
            cls.record_components = _parse_formal_params(params_node, source)

    body = node.child_by_field_name("body")
    if body is not None:
        for member in body.children:
            if member.type == "field_declaration":
                member_annotations, member_mods = _collect_annotations_and_modifiers(member, source)
                type_node = member.child_by_field_name("type")
                type_text = _text(type_node, source) if type_node is not None else "Object"
                for declarator in member.children:
                    if declarator.type == "variable_declarator":
                        field_name_node = declarator.child_by_field_name("name")
                        if field_name_node is not None:
                            cls.fields.append(
                                JavaField(
                                    name=_text(field_name_node, source),
                                    type_text=type_text,
                                    annotations=member_annotations,
                                    javadoc=_javadoc_before(member, source),
                                    line=member.start_point[0] + 1,
                                    modifiers=member_mods,
                                )
                            )
            elif member.type == "method_declaration":
                method_annotations, method_mods = _collect_annotations_and_modifiers(member, source)
                return_type_node = member.child_by_field_name("type")
                method_name_node = member.child_by_field_name("name")
                params_node = member.child_by_field_name("parameters")
                if method_name_node is None:
                    continue
                method = JavaMethod(
                    name=_text(method_name_node, source),
                    return_type=_text(return_type_node, source) if return_type_node is not None else "void",
                    params=_parse_formal_params(params_node, source) if params_node is not None else [],
                    annotations=method_annotations,
                    start_line=member.start_point[0] + 1,
                    end_line=member.end_point[0] + 1,
                    javadoc=_javadoc_before(member, source),
                    modifiers=method_mods,
                )
                throws_node = next((c for c in member.children if c.type == "throws"), None)
                if throws_node is not None:
                    method.throws_decl = [
                        part.strip()
                        for part in _text(throws_node, source).replace("throws", "", 1).split(",")
                    ]
                body_node = member.child_by_field_name("body")
                if body_node is not None:
                    method.body_text = _text(body_node, source)
                    _scan_method_body(body_node, source, method)
                cls.methods.append(method)
            elif member.type == "enum_body_declarations":
                for sub in member.children:
                    _parse_class(sub, source, package, rel_path, imports, wildcards, index, outer=class_name)
            elif member.type == "enum_constant":
                constant_name = member.child_by_field_name("name")
                if constant_name is not None:
                    cls.enum_constants.append(_text(constant_name, source))
            elif member.type in kind_by_type:
                _parse_class(member, source, package, rel_path, imports, wildcards, index, outer=class_name)
        if kind == "enum":
            # enum constants live inside enum_body directly
            for member in body.children:
                if member.type == "enum_constant":
                    constant_name = member.child_by_field_name("name")
                    if constant_name is not None and _text(constant_name, source) not in cls.enum_constants:
                        cls.enum_constants.append(_text(constant_name, source))
    index.add(cls)


def _split_generic_aware(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    for char in text:
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current)
    return [part.strip() for part in parts]


def scan_java_file(abs_path: Path, rel_path: str, index: JavaIndex) -> None:
    try:
        source = abs_path.read_bytes()
    except OSError as exc:
        log.debug("unreadable %s: %s", rel_path, exc)
        index.files_failed.append(rel_path)
        return
    tree = _parser().parse(source)
    root = tree.root_node
    if root.has_error:
        log.debug("syntax issues in %s (continuing, error-tolerant)", rel_path)

    package = ""
    imports: list[str] = []
    wildcards: list[str] = []
    for child in root.children:
        if child.type == "package_declaration":
            package = _text(child, source).replace("package", "", 1).strip().rstrip(";").strip()
        elif child.type == "import_declaration":
            text = _text(child, source).replace("import", "", 1).replace("static", "", 1).strip().rstrip(";").strip()
            if text.endswith(".*"):
                wildcards.append(text[:-2])
            else:
                imports.append(text)
    for child in root.children:
        _parse_class(child, source, package, rel_path, imports, wildcards, index)


def build_java_index(repo_root: Path, java_files: list[str]) -> JavaIndex:
    index = JavaIndex()
    for rel in java_files:
        scan_java_file(repo_root / rel, rel, index)
    log.info("java index: %d types from %d files", len(index.classes), len(java_files))
    return index
