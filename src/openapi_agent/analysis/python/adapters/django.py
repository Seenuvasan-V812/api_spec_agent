"""Django / Django REST Framework adapter.

Statically resolves the Django URL configuration (``ROOT_URLCONF``,
``include()`` graphs, DRF ``DefaultRouter``/``SimpleRouter`` registrations),
Django path converters and ``re_path`` regex routes, function views, DRF
``APIView``/generic views/ViewSets, and converts DRF serializers (declared
fields plus ``ModelSerializer`` Meta-driven model fields) to JSON Schema.

Security is emitted only when it is proven: ``permission_classes`` requiring
authentication combined with ``authentication_classes`` declared on the view
or in the ``REST_FRAMEWORK`` settings defaults. Unresolvable contracts degrade
to ``{}`` schemas with low confidence and an :class:`UnresolvedSite`; nothing
is invented.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field as dc_field

from astroid import nodes

from openapi_agent.analysis.base import (
    AnalysisContext,
    DetectionResult,
    FrameworkAdapter,
    OperationExtraction,
    RouteRef,
    UnresolvedSite,
)
from openapi_agent.analysis.python.context import PythonAnalysisContext, module_name_for
from openapi_agent.analysis.python.type_schema import (
    PyTypeSchemaConverter,
    dotted_name,
    literal_value,
)
from openapi_agent.detection.repo import RepoFacts
from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import (
    Condition,
    Confidence,
    DependencyEdge,
    Evidence,
    LangTypeRef,
    MediaTypeContract,
    Operation,
    Parameter,
    RequestBody,
    ResponseVariant,
    SecurityEvidence,
    SecuritySchemeDecl,
    Service,
)
from openapi_agent.models.registry import REF_PREFIX, make_pending_id

log = get_logger("analysis.python.django")

_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

_DJANGO_PARAM_RE = re.compile(r"<(?:([A-Za-z_][A-Za-z0-9_]*):)?([A-Za-z_][A-Za-z0-9_]*)>")
_STATUS_ATTR_RE = re.compile(r"HTTP_(\d{3})_")

_PATH_CONVERTERS: dict[str, dict] = {
    "int": {"type": "integer"},
    "str": {"type": "string"},
    "slug": {"type": "string"},
    "uuid": {"type": "string", "format": "uuid"},
    "path": {"type": "string"},
}

#: DRF authentication class short name -> (scheme_id, kind, detail)
_AUTH_CLASSES: dict[str, tuple[str, str, dict]] = {
    "TokenAuthentication": ("tokenAuth", "apikey_header", {"name": "Authorization"}),
    "SessionAuthentication": ("cookieAuth", "apikey_cookie", {"name": "sessionid"}),
    "BasicAuthentication": ("basicAuth", "http_basic", {}),
    "JWTAuthentication": ("jwtAuth", "http_bearer", {}),
}
_AUTH_REQUIRED_PERMISSIONS = {"IsAuthenticated", "IsAdminUser"}

#: standard viewset action -> (http method, is detail route)
_STANDARD_ACTIONS: dict[str, tuple[str, bool]] = {
    "list": ("get", False),
    "create": ("post", False),
    "retrieve": ("get", True),
    "update": ("put", True),
    "partial_update": ("patch", True),
    "destroy": ("delete", True),
}
_ACTION_ORDER = ("list", "create", "retrieve", "update", "partial_update", "destroy")

_VIEWSET_BASE_ACTIONS: dict[str, set[str]] = {
    "ModelViewSet": set(_ACTION_ORDER),
    "ReadOnlyModelViewSet": {"list", "retrieve"},
    "ListModelMixin": {"list"},
    "CreateModelMixin": {"create"},
    "RetrieveModelMixin": {"retrieve"},
    "UpdateModelMixin": {"update", "partial_update"},
    "DestroyModelMixin": {"destroy"},
}
_VIEWSET_BASES = {"ViewSet", "GenericViewSet", "ModelViewSet", "ReadOnlyModelViewSet", "ViewSetMixin"}

_GENERIC_VIEW_ACTIONS: dict[str, dict[str, str]] = {
    "ListAPIView": {"get": "list"},
    "CreateAPIView": {"post": "create"},
    "RetrieveAPIView": {"get": "retrieve"},
    "UpdateAPIView": {"put": "update", "patch": "partial_update"},
    "DestroyAPIView": {"delete": "destroy"},
    "ListCreateAPIView": {"get": "list", "post": "create"},
    "RetrieveUpdateAPIView": {"get": "retrieve", "put": "update", "patch": "partial_update"},
    "RetrieveDestroyAPIView": {"get": "retrieve", "delete": "destroy"},
    "RetrieveUpdateDestroyAPIView": {
        "get": "retrieve",
        "put": "update",
        "patch": "partial_update",
        "delete": "destroy",
    },
}

_SERIALIZER_BASES = {"Serializer", "ModelSerializer", "HyperlinkedModelSerializer", "ListSerializer"}

#: DRF serializer field class -> base JSON schema
_DRF_FIELD_TYPES: dict[str, dict] = {
    "CharField": {"type": "string"},
    "SlugField": {"type": "string"},
    "RegexField": {"type": "string"},
    "IntegerField": {"type": "integer"},
    "FloatField": {"type": "number"},
    "DecimalField": {"type": "number"},
    "BooleanField": {"type": "boolean"},
    "DateTimeField": {"type": "string", "format": "date-time"},
    "DateField": {"type": "string", "format": "date"},
    "TimeField": {"type": "string", "format": "time"},
    "DurationField": {"type": "string"},
    "UUIDField": {"type": "string", "format": "uuid"},
    "EmailField": {"type": "string", "format": "email"},
    "URLField": {"type": "string", "format": "uri"},
    "IPAddressField": {"type": "string"},
    "FileField": {"type": "string", "format": "uri"},
    "ImageField": {"type": "string", "format": "uri"},
    "ChoiceField": {"type": "string"},
    "MultipleChoiceField": {"type": "array"},
    "ListField": {"type": "array"},
    "DictField": {"type": "object"},
    "JSONField": {},
    "HStoreField": {"type": "object"},
    "PrimaryKeyRelatedField": {"type": "integer"},
    "StringRelatedField": {"type": "string"},
    "SlugRelatedField": {"type": "string"},
    "HyperlinkedRelatedField": {"type": "string", "format": "uri"},
    "HyperlinkedIdentityField": {"type": "string", "format": "uri"},
    "ReadOnlyField": {},
    "SerializerMethodField": {},
}
_DRF_CONSTRAINT_KWARGS = {
    "max_length": "maxLength",
    "min_length": "minLength",
    "max_value": "maximum",
    "min_value": "minimum",
}

_AUTO_MODEL_FIELDS = {"AutoField", "BigAutoField", "SmallAutoField"}

#: Django model field class -> base JSON schema
_MODEL_FIELD_TYPES: dict[str, dict] = {
    "AutoField": {"type": "integer"},
    "BigAutoField": {"type": "integer"},
    "SmallAutoField": {"type": "integer"},
    "IntegerField": {"type": "integer"},
    "BigIntegerField": {"type": "integer"},
    "SmallIntegerField": {"type": "integer"},
    "PositiveIntegerField": {"type": "integer"},
    "PositiveSmallIntegerField": {"type": "integer"},
    "PositiveBigIntegerField": {"type": "integer"},
    "FloatField": {"type": "number"},
    "DecimalField": {"type": "number"},
    "BooleanField": {"type": "boolean"},
    "CharField": {"type": "string"},
    "TextField": {"type": "string"},
    "SlugField": {"type": "string"},
    "EmailField": {"type": "string", "format": "email"},
    "URLField": {"type": "string", "format": "uri"},
    "UUIDField": {"type": "string", "format": "uuid"},
    "DateTimeField": {"type": "string", "format": "date-time"},
    "DateField": {"type": "string", "format": "date"},
    "TimeField": {"type": "string", "format": "time"},
    "DurationField": {"type": "string"},
    "GenericIPAddressField": {"type": "string"},
    "BinaryField": {"type": "string", "format": "binary"},
    "FileField": {"type": "string"},
    "ImageField": {"type": "string"},
    "JSONField": {"type": "object"},
    "ForeignKey": {"type": "integer"},
    "OneToOneField": {"type": "integer"},
}

_LEVEL_RANK = {"high": 0, "medium": 1, "low": 2}

_NOT_FOUND_SCHEMA = {
    "type": "object",
    "properties": {"detail": {"type": "string"}},
    "required": ["detail"],
}


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------


def _short(name: str | None) -> str:
    return (name or "").rsplit(".", 1)[-1]


def _kwargs_of(call: nodes.Call) -> dict[str, nodes.NodeNG]:
    return {k.arg: k.value for k in call.keywords or [] if k.arg}


def _lit_str(node) -> str | None:
    if node is None:
        return None
    ok, value = literal_value(node)
    return value if ok and isinstance(value, str) else None


def _lit_bool(node) -> bool:
    if node is None:
        return False
    ok, value = literal_value(node)
    return bool(value) if ok and isinstance(value, bool) else False


def _lit_str_list(node) -> list[str]:
    if node is None:
        return []
    ok, value = literal_value(node)
    if ok and isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _status_from_node(node) -> int | None:
    if node is None:
        return None
    ok, value = literal_value(node)
    if ok and isinstance(value, int) and not isinstance(value, bool):
        return value
    name = dotted_name(node)
    if name:
        match = _STATUS_ATTR_RE.search(name)
        if match:
            return int(match.group(1))
    return None


def _worse(a: Confidence, b: Confidence) -> Confidence:
    return a if _LEVEL_RANK[a.level] >= _LEVEL_RANK[b.level] else b


def _literal_schema(value) -> dict:
    """JSON Schema for a JSON-safe literal value (recursive)."""
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if value is None:
        return {"type": "null"}
    if isinstance(value, list):
        items = [_literal_schema(v) for v in value]
        if items and all(i == items[0] for i in items):
            return {"type": "array", "items": items[0]}
        return {"type": "array"}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {k: _literal_schema(v) for k, v in value.items() if isinstance(k, str)},
        }
    return {}


def _apply_extras(schema: dict, extras: dict) -> dict:
    """Attach descriptive keywords; wraps ``$ref`` schemas in ``allOf``."""
    if not extras:
        return schema
    if "$ref" in schema:
        return {"allOf": [schema], **extras}
    schema.update(extras)
    return schema


def _mark_nullable(schema: dict) -> dict:
    if "$ref" in schema:
        return {"anyOf": [schema, {"type": "null"}]}
    type_value = schema.get("type")
    if isinstance(type_value, str):
        schema["type"] = [type_value, "null"]
        return schema
    if schema:
        return {"anyOf": [schema, {"type": "null"}]}
    return schema


def _doc_hints(node) -> tuple[str | None, str | None]:
    doc = node.doc_node.value.strip() if node is not None and node.doc_node else None
    if not doc:
        return None, None
    lines = doc.splitlines()
    summary = lines[0].strip() or None
    description = "\n".join(line.strip() for line in lines[1:]).strip() or None
    return summary, description


def _build_system(facts: RepoFacts) -> str | None:
    kinds = {m.kind for m in facts.manifests}
    if "pyproject" in kinds:
        return "pyproject"
    if "requirements" in kinds:
        return "pip"
    return None


def _repo_mro(cls: nodes.ClassDef, converter: PyTypeSchemaConverter, _seen: set | None = None) -> list[nodes.ClassDef]:
    """[cls, base, base-of-base, ...] restricted to repo-resolvable classes."""
    seen = _seen if _seen is not None else set()
    try:
        qname = cls.qname()
    except Exception:  # noqa: BLE001
        return []
    if qname in seen:
        return []
    seen.add(qname)
    result = [cls]
    module = cls.root()
    for base in cls.bases:
        name = dotted_name(base if not isinstance(base, nodes.Subscript) else base.value)
        if not name:
            continue
        try:
            resolved = converter.resolve_symbol(module, name)
        except Exception:  # noqa: BLE001
            resolved = None
        if isinstance(resolved, nodes.ClassDef):
            result.extend(_repo_mro(resolved, converter, seen))
    return result


def _all_base_shorts(cls: nodes.ClassDef, converter: PyTypeSchemaConverter) -> set[str]:
    shorts: set[str] = set()
    for klass in _repo_mro(cls, converter):
        for base in klass.bases:
            name = dotted_name(base if not isinstance(base, nodes.Subscript) else base.value)
            if name:
                shorts.add(_short(name))
    return shorts


def _class_attr_value(
    cls: nodes.ClassDef, converter: PyTypeSchemaConverter, name: str
) -> tuple[nodes.NodeNG, nodes.ClassDef, int, int] | None:
    """First class-body assignment ``name = ...`` found along the repo MRO."""
    for klass in _repo_mro(cls, converter):
        for stmt in klass.body:
            if isinstance(stmt, nodes.Assign):
                for target in stmt.targets:
                    if isinstance(target, nodes.AssignName) and target.name == name:
                        return stmt.value, klass, stmt.lineno or 1, stmt.end_lineno or stmt.lineno or 1
            elif (
                isinstance(stmt, nodes.AnnAssign)
                and isinstance(stmt.target, nodes.AssignName)
                and stmt.target.name == name
                and stmt.value is not None
            ):
                return stmt.value, klass, stmt.lineno or 1, stmt.end_lineno or stmt.lineno or 1
    return None


def _find_method(
    cls: nodes.ClassDef, converter: PyTypeSchemaConverter, name: str
) -> nodes.FunctionDef | None:
    for klass in _repo_mro(cls, converter):
        for stmt in klass.body:
            if isinstance(stmt, nodes.FunctionDef) and stmt.name == name:
                return stmt
    return None


def _is_serializer_class(cls: nodes.ClassDef, converter: PyTypeSchemaConverter) -> bool:
    return bool(_all_base_shorts(cls, converter) & _SERIALIZER_BASES)


def _name_shorts(node) -> list[str]:
    """Short class names inside a literal list/tuple of names or calls."""
    shorts: list[str] = []
    if isinstance(node, (nodes.List, nodes.Tuple)):
        for element in node.elts:
            target = element.func if isinstance(element, nodes.Call) else element
            name = dotted_name(target)
            if name:
                shorts.append(_short(name))
    return shorts


def _root_name(node) -> str | None:
    """Left-most Name in an Attribute/Call chain (``Pet.objects.all()`` -> ``Pet``)."""
    current = node
    while True:
        if isinstance(current, nodes.Call):
            current = current.func
        elif isinstance(current, nodes.Attribute):
            current = current.expr
        elif isinstance(current, nodes.Name):
            return current.name
        else:
            return None


def _regex_template(pattern: str) -> tuple[str, list[str]]:
    """Best-effort URL template from a regex; named groups become ``{name}``."""
    s = pattern
    if s.startswith("^"):
        s = s[1:]
    if s.endswith("$"):
        s = s[:-1]
    out: list[str] = []
    names: list[str] = []
    i = 0
    while i < len(s):
        if s.startswith("(?P<", i):
            close = s.find(">", i)
            if close == -1:
                out.append(s[i])
                i += 1
                continue
            name = s[i + 4 : close]
            depth = 1
            j = close + 1
            while j < len(s) and depth:
                if s[j] == "\\":
                    j += 2
                    continue
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                j += 1
            out.append("{" + name + "}")
            names.append(name)
            i = j
        elif s[i] == "\\":
            if i + 1 < len(s):
                out.append(s[i + 1])
            i += 2
        elif s[i] in "()?*+^$":
            i += 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out), names


# ---------------------------------------------------------------------------
# state records
# ---------------------------------------------------------------------------


@dataclass
class AuthDecl:
    scheme_id: str
    kind: str
    detail: dict
    evidence: Evidence


@dataclass
class DjangoState:
    service_id: str
    settings_file: str
    urlconf: str | None = None
    default_auth: list[AuthDecl] = dc_field(default_factory=list)
    default_permissions: set[str] = dc_field(default_factory=set)
    default_pagination: bool = False
    settings_evidence: Evidence | None = None
    route_details: dict[tuple[str, str], dict] = dc_field(default_factory=dict)


@dataclass
class SerializerInfo:
    ref_schema: dict
    confidence: Confidence
    unresolved_fields: tuple[str, ...] = ()


@dataclass
class MethodScan:
    returns: list = dc_field(default_factory=list)  # (status str, schema|None, origin, Confidence|None, Evidence)
    request_serializer: nodes.ClassDef | None = None
    request_partial: bool = False
    has_validation: bool = False
    has_get_object: bool = False


# ---------------------------------------------------------------------------
# DRF serializer -> JSON Schema
# ---------------------------------------------------------------------------


class DrfSerializerConverter:
    """Converts DRF serializer classes into interned registry schemas."""

    def __init__(self, ctx: PythonAnalysisContext, service_id: str, converter: PyTypeSchemaConverter) -> None:
        self.ctx = ctx
        self.service_id = service_id
        self.converter = converter
        self._cache: dict[tuple[str, bool], SerializerInfo] = {}
        self._raw: dict[str, dict] = {}
        self._building: set[str] = set()

    # -- public -----------------------------------------------------------
    def convert(self, cls: nodes.ClassDef, partial: bool = False) -> SerializerInfo:
        try:
            qname = cls.qname()
        except Exception:  # noqa: BLE001
            return SerializerInfo({}, Confidence(level="low", reason_code="unresolved_symbol"))
        key = (qname, partial)
        if key in self._cache:
            cached = self._cache[key]
            return SerializerInfo(dict(cached.ref_schema), cached.confidence, cached.unresolved_fields)
        if qname in self._building:
            lang = LangTypeRef(language="python", qualified_name=qname)
            return SerializerInfo(
                {"$ref": REF_PREFIX + make_pending_id(lang)},
                Confidence(level="medium", reason_code="inferred_serializer"),
            )
        if partial:
            full = self.convert(cls, partial=False)
            raw = copy.deepcopy(self._raw.get(qname) or {})
            raw.pop("required", None)
            evidence = self._class_evidence(cls, qname)
            ref = self.ctx.registry.intern(
                None,
                raw,
                evidence,
                full.confidence,
                self.service_id,
                title=f"Patched{cls.name}",
                synthetic_name=f"py.{qname}.PartialUpdate",
            )
            info = SerializerInfo({"$ref": ref}, full.confidence, full.unresolved_fields)
            self._cache[key] = info
            return SerializerInfo(dict(info.ref_schema), info.confidence, info.unresolved_fields)

        self._building.add(qname)
        try:
            schema, confidence, unresolved = self._build(cls)
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            log.debug("serializer conversion failed for %s: %s", qname, exc)
            schema = {"type": "object", "additionalProperties": True}
            confidence = Confidence(level="low", reason_code="dynamic_type")
            unresolved = []
        finally:
            self._building.discard(qname)
        self._raw[qname] = schema
        lang = LangTypeRef(language="python", qualified_name=qname)
        ref = self.ctx.registry.intern(
            lang, schema, self._class_evidence(cls, qname), confidence, self.service_id
        )
        info = SerializerInfo({"$ref": ref}, confidence, tuple(unresolved))
        self._cache[key] = info
        return SerializerInfo(dict(info.ref_schema), info.confidence, info.unresolved_fields)

    # -- internals ----------------------------------------------------------
    def _class_evidence(self, cls: nodes.ClassDef, qname: str) -> list[Evidence]:
        module = cls.root()
        rel = self.ctx.rel(module.file) if module.file else "unknown"
        return [
            Evidence(
                file=rel,
                start_line=cls.lineno or 1,
                end_line=cls.end_lineno or cls.lineno or 1,
                kind="class_def",
                symbol=qname,
            )
        ]

    def _build(self, cls: nodes.ClassDef) -> tuple[dict, Confidence, list[str]]:
        worst = ["high", "declared_type"]

        def degrade(level: str, reason: str) -> None:
            if _LEVEL_RANK[level] > _LEVEL_RANK[worst[0]]:
                worst[0], worst[1] = level, reason

        unresolved: list[str] = []
        declared: dict[str, dict] = {}
        order: list[str] = []
        for klass in reversed(_repo_mro(cls, self.converter)):
            for stmt in klass.body:
                if not isinstance(stmt, nodes.Assign) or len(stmt.targets) != 1:
                    continue
                target = stmt.targets[0]
                if not isinstance(target, nodes.AssignName) or not isinstance(stmt.value, nodes.Call):
                    continue
                entry = self._declared_field(target.name, stmt.value, klass, degrade, unresolved)
                if entry is None:
                    continue
                if target.name not in declared:
                    order.append(target.name)
                declared[target.name] = entry

        fields_spec, model_cls, read_only_names, exclude = self._meta_info(cls, degrade)

        properties: dict[str, dict] = {}
        required: list[str] = []

        if fields_spec is not None or exclude is not None:
            degrade("medium", "inferred_serializer")
            model_fields: dict[str, dict] = {}
            model_order: list[str] = []
            if model_cls is not None:
                model_fields, model_order = self._model_fields(model_cls, degrade)
            if fields_spec == "__all__" or exclude is not None:
                names = ["id"] if "id" not in model_fields else []
                names += model_order
                names += [n for n in order if n not in names]
                if exclude:
                    names = [n for n in names if n not in exclude]
            else:
                names = list(fields_spec or [])
            for name in names:
                if name in declared:
                    entry = declared[name]
                elif name in model_fields:
                    entry = model_fields[name]
                elif name in ("id", "pk"):
                    entry = {"schema": {"type": "integer", "readOnly": True}, "required": False}
                else:
                    degrade("low", "dynamic_type")
                    unresolved.append(name)
                    entry = {"schema": {}, "required": False}
                schema = dict(entry["schema"]) if isinstance(entry["schema"], dict) else entry["schema"]
                is_required = entry["required"]
                if name in read_only_names:
                    schema = _apply_extras(schema, {"readOnly": True})
                    is_required = False
                properties[name] = schema
                if is_required:
                    required.append(name)
        else:
            for name in order:
                entry = declared[name]
                properties[name] = entry["schema"]
                if entry["required"]:
                    required.append(name)

        schema: dict = {"type": "object", "properties": properties}
        if required:
            schema["required"] = sorted(required)
        doc = cls.doc_node.value.strip().splitlines()[0].strip() if cls.doc_node else None
        if doc:
            schema["description"] = doc
        if not properties:
            degrade("low", "dynamic_type")
            schema = {"type": "object", "additionalProperties": True}
        return schema, Confidence(level=worst[0], reason_code=worst[1]), unresolved  # type: ignore[arg-type]

    def _meta_info(self, cls: nodes.ClassDef, degrade):
        meta = None
        for klass in _repo_mro(cls, self.converter):
            for stmt in klass.body:
                if isinstance(stmt, nodes.ClassDef) and stmt.name == "Meta":
                    meta = stmt
                    break
            if meta is not None:
                break
        if meta is None:
            return None, None, set(), None
        fields_spec = None
        model_cls = None
        read_only: set[str] = set()
        exclude: list[str] | None = None
        for stmt in meta.body:
            if not isinstance(stmt, nodes.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not isinstance(target, nodes.AssignName):
                continue
            if target.name == "model":
                name = dotted_name(stmt.value)
                if name:
                    resolved = self.converter.resolve_symbol(meta.root(), name)
                    if isinstance(resolved, nodes.ClassDef):
                        model_cls = resolved
                    else:
                        degrade("low", "unresolved_symbol")
            elif target.name == "fields":
                literal = _lit_str(stmt.value)
                if literal == "__all__":
                    fields_spec = "__all__"
                else:
                    listed = _lit_str_list(stmt.value)
                    if listed:
                        fields_spec = listed
            elif target.name == "exclude":
                exclude = _lit_str_list(stmt.value)
            elif target.name == "read_only_fields":
                read_only = set(_lit_str_list(stmt.value))
        return fields_spec, model_cls, read_only, exclude

    # -- declared DRF fields -------------------------------------------------
    def _declared_field(
        self,
        name: str,
        call: nodes.Call,
        klass: nodes.ClassDef,
        degrade,
        unresolved: list[str],
    ) -> dict | None:
        func_name = dotted_name(call.func) or ""
        short = _short(func_name)
        kwargs = _kwargs_of(call)
        module = klass.root()

        if short == "SerializerMethodField":
            method_name = _lit_str(kwargs.get("method_name")) or f"get_{name}"
            method = _find_method(klass, self.converter, method_name)
            if method is not None and method.returns is not None:
                converted, conf = self.converter.convert(method.returns, method.root())
                degrade("medium", "inferred_return_flow")
                degrade(conf.level, conf.reason_code)
                schema = _apply_extras(converted, {"readOnly": True})
            else:
                degrade("low", "dynamic_type")
                unresolved.append(name)
                schema = {"readOnly": True}
            return {"schema": schema, "required": False}

        schema: dict | None = None
        if short in _DRF_FIELD_TYPES:
            schema = dict(_DRF_FIELD_TYPES[short])
            if short in ("ListField", "MultipleChoiceField"):
                child = kwargs.get("child")
                items: dict = {}
                if isinstance(child, nodes.Call):
                    child_entry = self._declared_field(name, child, klass, degrade, [])
                    if child_entry is not None:
                        items = child_entry["schema"]
                if items:
                    schema["items"] = items
            if short in ("ChoiceField", "MultipleChoiceField"):
                values = self._choices(kwargs.get("choices"), klass)
                if values is not None:
                    enum_schema = self._enum_schema(values)
                    if short == "MultipleChoiceField":
                        schema = {"type": "array", "items": enum_schema, "uniqueItems": True}
                    else:
                        schema = enum_schema
                else:
                    degrade("medium", "unresolved_symbol")
        else:
            resolved = self.converter.resolve_symbol(module, func_name)
            if isinstance(resolved, nodes.ClassDef) and _is_serializer_class(resolved, self.converter):
                nested = self.convert(resolved)
                degrade(nested.confidence.level, nested.confidence.reason_code)
                unresolved.extend(f"{name}.{u}" for u in nested.unresolved_fields)
                schema = nested.ref_schema
                if _lit_bool(kwargs.get("many")):
                    schema = {"type": "array", "items": schema}
            else:
                return None  # not a serializer field declaration

        extras: dict = {}
        for kwarg_name, json_key in _DRF_CONSTRAINT_KWARGS.items():
            node = kwargs.get(kwarg_name)
            if node is not None and "$ref" not in schema:
                ok, value = literal_value(node)
                if ok and value is not None:
                    extras[json_key] = value
        help_text = _lit_str(kwargs.get("help_text"))
        if help_text:
            extras["description"] = help_text

        required = True
        read_only = _lit_bool(kwargs.get("read_only"))
        required_node = kwargs.get("required")
        if required_node is not None:
            ok, value = literal_value(required_node)
            if ok and value is False:
                required = False
        if "default" in kwargs:
            required = False
            ok, value = literal_value(kwargs["default"])
            if ok and "$ref" not in schema and not isinstance(kwargs["default"], nodes.Call):
                extras.setdefault("default", value)
        if read_only:
            extras["readOnly"] = True
            required = False
        if _lit_bool(kwargs.get("write_only")):
            extras["writeOnly"] = True
        schema = _apply_extras(schema, extras)
        if _lit_bool(kwargs.get("allow_null")):
            schema = _mark_nullable(schema)
        return {"schema": schema, "required": required}

    def _enum_schema(self, values: list) -> dict:
        schema: dict = {"enum": values}
        if all(isinstance(v, str) for v in values):
            schema["type"] = "string"
        elif all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            schema["type"] = "integer"
        return schema

    def _choices(self, node, owner_cls: nodes.ClassDef) -> list | None:
        raw = self._resolve_literal(node, owner_cls)
        if not isinstance(raw, list) or not raw:
            return None
        out: list = []
        for item in raw:
            if isinstance(item, list) and len(item) == 2:
                out.append(item[0])
            elif isinstance(item, (str, int, float)) and not isinstance(item, bool):
                out.append(item)
            else:
                return None
        return out or None

    def _resolve_literal(self, node, owner_cls: nodes.ClassDef):
        """Literal value of a node, following Name/Attribute assignments."""
        if node is None:
            return None
        ok, value = literal_value(node)
        if ok:
            return value
        name = dotted_name(node)
        if not name:
            return None
        parts = name.split(".")

        def value_in_class(klass: nodes.ClassDef, attr: str):
            for stmt in klass.body:
                if isinstance(stmt, nodes.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                    if isinstance(target, nodes.AssignName) and target.name == attr:
                        return stmt.value
            return None

        candidate = None
        if len(parts) == 1:
            candidate = value_in_class(owner_cls, parts[0])
            if candidate is None:
                module = owner_cls.root()
                for stmt in module.body:
                    if isinstance(stmt, nodes.Assign) and len(stmt.targets) == 1:
                        target = stmt.targets[0]
                        if isinstance(target, nodes.AssignName) and target.name == parts[0]:
                            candidate = stmt.value
                            break
        else:
            resolved = self.converter.resolve_symbol(owner_cls.root(), ".".join(parts[:-1]))
            if isinstance(resolved, nodes.ClassDef):
                candidate = value_in_class(resolved, parts[-1])
        if candidate is None:
            return None
        ok, value = literal_value(candidate)
        return value if ok else None

    # -- Django model fields ---------------------------------------------------
    def _model_fields(self, model_cls: nodes.ClassDef, degrade) -> tuple[dict[str, dict], list[str]]:
        fields: dict[str, dict] = {}
        order: list[str] = []
        for klass in reversed(_repo_mro(model_cls, self.converter)):
            for stmt in klass.body:
                if not isinstance(stmt, nodes.Assign) or len(stmt.targets) != 1:
                    continue
                target = stmt.targets[0]
                if not isinstance(target, nodes.AssignName) or not isinstance(stmt.value, nodes.Call):
                    continue
                short = _short(dotted_name(stmt.value.func))
                if short != "ManyToManyField" and short not in _MODEL_FIELD_TYPES:
                    continue
                entry = self._model_field_entry(short, stmt.value, klass)
                if target.name not in fields:
                    order.append(target.name)
                fields[target.name] = entry
        return fields, order

    def _model_field_entry(self, short: str, call: nodes.Call, klass: nodes.ClassDef) -> dict:
        kwargs = _kwargs_of(call)
        if short == "ManyToManyField":
            schema: dict = {"type": "array", "items": {"type": "integer"}}
        else:
            schema = dict(_MODEL_FIELD_TYPES[short])
        extras: dict = {}
        required = True
        read_only = short in _AUTO_MODEL_FIELDS
        if _lit_bool(kwargs.get("primary_key")):
            read_only = True
        if _lit_bool(kwargs.get("auto_now")) or _lit_bool(kwargs.get("auto_now_add")):
            read_only = True
        max_length = kwargs.get("max_length")
        if max_length is not None and schema.get("type") == "string":
            ok, value = literal_value(max_length)
            if ok and isinstance(value, int):
                extras["maxLength"] = value
        values = self._choices(kwargs.get("choices"), klass)
        if values is not None:
            enum_schema = self._enum_schema(values)
            enum_schema.update({k: v for k, v in schema.items() if k not in enum_schema})
            schema = enum_schema
        nullable = _lit_bool(kwargs.get("null"))
        if nullable:
            required = False
        if _lit_bool(kwargs.get("blank")):
            required = False
        if "default" in kwargs:
            required = False
            ok, value = literal_value(kwargs["default"])
            if ok and not isinstance(kwargs["default"], nodes.Call):
                extras.setdefault("default", value)
        help_text = _lit_str(kwargs.get("help_text"))
        if help_text:
            extras["description"] = help_text
        if read_only:
            extras["readOnly"] = True
            required = False
        schema = _apply_extras(schema, extras)
        if nullable:
            schema = _mark_nullable(schema)
        return {"schema": schema, "required": required}


# ---------------------------------------------------------------------------
# the adapter
# ---------------------------------------------------------------------------


class DjangoAdapter(FrameworkAdapter):
    name = "django"
    language = "python"

    def can_handle(self, facts: RepoFacts) -> DetectionResult:
        score = 0.0
        rationale = []
        deps = facts.manifest_dep_names()
        if "django" in deps:
            score += 0.5
            rationale.append("django in manifest")
        if "djangorestframework" in deps:
            score += 0.2
            rationale.append("DRF in manifest")
        if facts.import_hits.get("django"):
            score += 0.25
            rationale.append("django imported")
        if any(f.endswith(("settings.py", "urls.py")) for f in facts.config_files):
            score += 0.1
            rationale.append("settings.py/urls.py present")
        return DetectionResult(score=min(score, 0.95), rationale="; ".join(rationale))

    # -------------------------------------------------------------- services
    def discover_services(self, ctx: AnalysisContext) -> list[Service]:
        assert isinstance(ctx, PythonAnalysisContext)
        services: list[Service] = []
        states: dict[str, DjangoState] = {}
        used_ids: set[str] = set()
        settings_files = sorted(f for f in ctx.repo_facts.config_files if f.endswith("settings.py"))
        for rel in settings_files:
            module = ctx.astroid_module(rel)
            if module is None:
                continue
            project_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
            base = project_dir.rsplit("/", 1)[-1] or "django-app"
            service_id = base
            n = 1
            while service_id in used_ids:
                n += 1
                service_id = f"{base}-{n}"
            used_ids.add(service_id)

            state = DjangoState(service_id=service_id, settings_file=rel)
            self._parse_settings(module, rel, state)
            if state.urlconf is None:
                candidate = f"{project_dir}/urls.py".lstrip("/")
                if (ctx.repo_root / candidate).is_file():
                    state.urlconf = module_name_for(candidate)
            if state.urlconf is None:
                ctx.warnings.emit(
                    "W201",
                    "django settings found but no ROOT_URLCONF or adjacent urls.py",
                    file=rel,
                    service_id=service_id,
                )
            root_path = project_dir.rsplit("/", 1)[0] if "/" in project_dir else ""
            services.append(
                Service(
                    id=service_id,
                    name=service_id,
                    language="python",
                    framework="django",
                    framework_version=ctx.repo_facts.dep_version("django"),
                    build_system=_build_system(ctx.repo_facts),
                    root_path=root_path,
                )
            )
            states[service_id] = state
        if not services:
            ctx.warnings.emit("W201", "django detected but no settings.py module found")
        ctx.extras["django_states"] = states
        return services

    def _parse_settings(self, module: nodes.Module, rel: str, state: DjangoState) -> None:
        for stmt in module.body:
            if not isinstance(stmt, nodes.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not isinstance(target, nodes.AssignName):
                continue
            if target.name == "ROOT_URLCONF":
                literal = _lit_str(stmt.value)
                if literal:
                    state.urlconf = literal
            elif target.name == "REST_FRAMEWORK" and isinstance(stmt.value, nodes.Dict):
                evidence = Evidence(
                    file=rel,
                    start_line=stmt.lineno or 1,
                    end_line=stmt.end_lineno or stmt.lineno or 1,
                    kind="config_file",
                    symbol="REST_FRAMEWORK",
                )
                state.settings_evidence = evidence
                for key_node, value_node in stmt.value.items:
                    key = _lit_str(key_node)
                    if key == "DEFAULT_AUTHENTICATION_CLASSES":
                        for dotted in _lit_str_list(value_node):
                            short = _short(dotted)
                            if short in _AUTH_CLASSES:
                                scheme_id, kind, detail = _AUTH_CLASSES[short]
                                state.default_auth.append(
                                    AuthDecl(scheme_id, kind, dict(detail), evidence)
                                )
                    elif key == "DEFAULT_PERMISSION_CLASSES":
                        state.default_permissions = {
                            _short(dotted) for dotted in _lit_str_list(value_node)
                        }
                    elif key == "DEFAULT_PAGINATION_CLASS":
                        if _lit_str(value_node):
                            state.default_pagination = True

    # ---------------------------------------------------------------- routes
    def discover_routes(self, ctx: AnalysisContext, service: Service) -> list[RouteRef]:
        assert isinstance(ctx, PythonAnalysisContext)
        state: DjangoState = ctx.extras["django_states"][service.id]
        converter = PyTypeSchemaConverter(ctx, service.id)
        ser_conv = DrfSerializerConverter(ctx, service.id, converter)
        ctx.extras.setdefault("django_tools", {})[service.id] = (converter, ser_conv)
        routes: list[RouteRef] = []
        if state.urlconf:
            self._walk_urlconf(ctx, service, state, converter, state.urlconf, (), set(), routes)
        return routes

    def _walk_urlconf(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        state: DjangoState,
        converter: PyTypeSchemaConverter,
        modname: str,
        prefix: tuple[tuple[str, bool], ...],
        visited: set,
        routes: list[RouteRef],
    ) -> None:
        key = (modname, prefix)
        if key in visited:
            return
        visited.add(key)
        module = ctx.module_by_name(modname)
        if module is None:
            ctx.warnings.emit(
                "W202", f"urlconf module {modname!r} could not be resolved", service_id=service.id
            )
            return
        rel = ctx.rel(module.file) if module.file else "unknown"
        pattern_calls = self._urlpatterns_calls(module)
        if not pattern_calls:
            ctx.warnings.emit(
                "W201", f"no urlpatterns found in {modname}", file=rel, service_id=service.id
            )
        for call in pattern_calls:
            try:
                self._process_pattern(
                    ctx, service, state, converter, module, rel, call, prefix, visited, routes
                )
            except Exception as exc:  # noqa: BLE001 - one broken pattern must not sink the run
                log.debug("pattern processing failed in %s: %s", rel, exc)
                ctx.warnings.emit(
                    "W202",
                    f"url pattern could not be analyzed: {exc}",
                    file=rel,
                    start_line=call.lineno,
                    service_id=service.id,
                )

    @staticmethod
    def _urlpatterns_calls(module: nodes.Module) -> list[nodes.Call]:
        calls: list[nodes.Call] = []

        def collect(node) -> None:
            if isinstance(node, (nodes.List, nodes.Tuple)):
                for element in node.elts:
                    if isinstance(element, nodes.Call):
                        calls.append(element)
            elif isinstance(node, nodes.BinOp) and node.op == "+":
                collect(node.left)
                collect(node.right)

        for stmt in module.body:
            if isinstance(stmt, nodes.Assign):
                if any(
                    isinstance(t, nodes.AssignName) and t.name == "urlpatterns"
                    for t in stmt.targets
                ):
                    collect(stmt.value)
            elif isinstance(stmt, nodes.AugAssign):
                target = stmt.target
                if isinstance(target, nodes.AssignName) and target.name == "urlpatterns":
                    collect(stmt.value)
        return calls

    def _process_pattern(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        state: DjangoState,
        converter: PyTypeSchemaConverter,
        module: nodes.Module,
        rel: str,
        call: nodes.Call,
        prefix: tuple[tuple[str, bool], ...],
        visited: set,
        routes: list[RouteRef],
    ) -> None:
        func_short = _short(dotted_name(call.func))
        if func_short not in ("path", "re_path", "url"):
            return
        if not call.args:
            return
        route_lit = _lit_str(call.args[0])
        if route_lit is None:
            ctx.warnings.emit(
                "W204",
                "url route is not a literal string; endpoint skipped",
                file=rel,
                start_line=call.lineno,
                service_id=service.id,
            )
            return
        is_regex = func_short in ("re_path", "url")
        if len(call.args) < 2:
            return
        view = call.args[1]

        if isinstance(view, nodes.Call) and _short(dotted_name(view.func)) == "include":
            self._handle_include(
                ctx, service, state, converter, module, rel, call, view,
                prefix + ((route_lit, is_regex),), visited, routes,
            )
            return
        self._register_view(
            ctx, service, state, converter, module, rel, call, view,
            prefix, route_lit, is_regex, routes,
        )

    def _handle_include(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        state: DjangoState,
        converter: PyTypeSchemaConverter,
        module: nodes.Module,
        rel: str,
        call: nodes.Call,
        include_call: nodes.Call,
        new_prefix: tuple[tuple[str, bool], ...],
        visited: set,
        routes: list[RouteRef],
    ) -> None:
        if not include_call.args:
            return
        target = include_call.args[0]
        if isinstance(target, nodes.Tuple) and target.elts:
            target = target.elts[0]  # include((patterns_or_module, namespace))
        if isinstance(target, nodes.Const) and isinstance(target.value, str):
            included = ctx.module_by_name(target.value)
            if included is not None and included.file:
                service.dependencies.append(
                    DependencyEdge(from_file=rel, to_file=ctx.rel(included.file), kind="url_include")
                )
            self._walk_urlconf(
                ctx, service, state, converter, target.value, new_prefix, visited, routes
            )
            return
        if isinstance(target, nodes.List):
            for element in target.elts:
                if isinstance(element, nodes.Call):
                    self._process_pattern(
                        ctx, service, state, converter, module, rel, element,
                        new_prefix, visited, routes,
                    )
            return
        if isinstance(target, nodes.Attribute) and target.attrname == "urls":
            router_name = dotted_name(target.expr)
            registrations = self._router_registrations(module, router_name, converter)
            if registrations is None:
                ctx.warnings.emit(
                    "W202",
                    f"router {router_name!r} referenced by include() could not be resolved",
                    file=rel,
                    start_line=call.lineno,
                    service_id=service.id,
                )
                return
            for reg_prefix, viewset, register_call in registrations:
                self._register_viewset_routes(
                    ctx, service, state, converter, rel, register_call,
                    viewset, reg_prefix, new_prefix, routes,
                )
            return
        ctx.warnings.emit(
            "W202",
            "include() target could not be resolved statically",
            file=rel,
            start_line=call.lineno,
            service_id=service.id,
        )

    def _router_registrations(
        self, module: nodes.Module, router_name: str | None, converter: PyTypeSchemaConverter
    ) -> list[tuple[str, nodes.ClassDef, nodes.Call]] | None:
        if not router_name:
            return None
        found_router = False
        for stmt in module.nodes_of_class(nodes.Assign):
            if not isinstance(stmt.value, nodes.Call) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not isinstance(target, nodes.AssignName) or target.name != router_name:
                continue
            if _short(dotted_name(stmt.value.func)) in ("DefaultRouter", "SimpleRouter"):
                found_router = True
        if not found_router:
            return None
        registrations: list[tuple[str, nodes.ClassDef, nodes.Call]] = []
        for call in module.nodes_of_class(nodes.Call):
            func = call.func
            if not isinstance(func, nodes.Attribute) or func.attrname != "register":
                continue
            if dotted_name(func.expr) != router_name:
                continue
            if len(call.args) < 2:
                continue
            reg_prefix = _lit_str(call.args[0])
            viewset_name = dotted_name(call.args[1])
            if reg_prefix is None or not viewset_name:
                continue
            resolved = converter.resolve_symbol(module, viewset_name)
            if isinstance(resolved, nodes.ClassDef):
                registrations.append((reg_prefix.strip("^$/"), resolved, call))
        return registrations

    # -- route registration ----------------------------------------------------

    def _convert_segment(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        rel: str,
        line: int,
        text: str,
        is_regex: bool,
    ) -> tuple[str, list[tuple[str, dict, Confidence]]]:
        """(template text, path params) for one URL segment."""
        if is_regex:
            template, names = _regex_template(text)
            params = [
                (
                    name,
                    {"type": "string"},
                    Confidence(level="medium", reason_code="framework_default"),
                )
                for name in names
            ]
            return template.lstrip("/"), params

        params: list[tuple[str, dict, Confidence]] = []

        def repl(match: re.Match) -> str:
            conv, name = match.group(1), match.group(2)
            conv = conv or "str"
            if conv in _PATH_CONVERTERS:
                schema = dict(_PATH_CONVERTERS[conv])
                confidence = Confidence(level="high", reason_code="declared_annotation")
            else:
                schema = {"type": "string"}
                confidence = Confidence(level="low", reason_code="framework_default")
                ctx.warnings.emit(
                    "W205",
                    f"unknown path converter {conv!r}; parameter typed as string",
                    file=rel,
                    start_line=line,
                    service_id=service.id,
                )
            params.append((name, schema, confidence))
            return "{" + name + "}"

        template = _DJANGO_PARAM_RE.sub(repl, text.lstrip("/"))
        return template, params

    def _compose_path(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        rel: str,
        line: int,
        prefix: tuple[tuple[str, bool], ...],
        segment: str,
        is_regex: bool,
    ) -> tuple[str, str, list[tuple[str, dict, Confidence]]]:
        """(normalized template, raw path, params) for prefix + final segment."""
        template_parts: list[str] = []
        raw_parts: list[str] = []
        params: list[tuple[str, dict, Confidence]] = []
        for text, seg_regex in prefix + ((segment, is_regex),):
            converted, seg_params = self._convert_segment(ctx, service, rel, line, text, seg_regex)
            template_parts.append(converted)
            raw_parts.append(text.lstrip("/"))
            params.extend(seg_params)
        template = re.sub(r"/{2,}", "/", "/" + "".join(template_parts))
        raw = re.sub(r"/{2,}", "/", "/" + "".join(raw_parts))
        return template, raw, params

    def _register_view(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        state: DjangoState,
        converter: PyTypeSchemaConverter,
        module: nodes.Module,
        rel: str,
        call: nodes.Call,
        view: nodes.NodeNG,
        prefix: tuple[tuple[str, bool], ...],
        route_lit: str,
        is_regex: bool,
        routes: list[RouteRef],
    ) -> None:
        line = call.lineno or 1
        template, raw, params = self._compose_path(
            ctx, service, rel, line, prefix, route_lit, is_regex
        )

        cls: nodes.ClassDef | None = None
        func: nodes.FunctionDef | None = None
        as_view_actions: dict[str, str] | None = None
        if isinstance(view, nodes.Call) and isinstance(view.func, nodes.Attribute) and view.func.attrname == "as_view":
            owner = dotted_name(view.func.expr)
            resolved = converter.resolve_symbol(module, owner or "")
            if isinstance(resolved, nodes.ClassDef):
                cls = resolved
            if view.args and isinstance(view.args[0], nodes.Dict):
                ok, mapping = literal_value(view.args[0])
                if ok and isinstance(mapping, dict):
                    as_view_actions = {
                        str(k).lower(): str(v) for k, v in mapping.items()
                    }
        else:
            name = dotted_name(view)
            resolved = converter.resolve_symbol(module, name or "") if name else None
            if isinstance(resolved, nodes.FunctionDef):
                func = resolved
            elif isinstance(resolved, nodes.ClassDef):
                cls = resolved

        if func is not None:
            methods = self._function_view_methods(func)
            handler = f"{func.root().name}.{func.name}"
            routes.append(
                RouteRef(
                    service_hint=service.id,
                    raw_path=raw,
                    methods=methods,
                    handler_symbol=handler,
                    file=rel,
                    start_line=line,
                    kind="urlconf",
                )
            )
            state.route_details[(handler, raw)] = {
                "type": "function",
                "func": func,
                "path": template,
                "params": params,
                "tags": [func.root().name.split(".")[0]],
            }
            return
        if cls is None:
            ctx.warnings.emit(
                "W202",
                "view reference could not be resolved statically; endpoint skipped",
                file=rel,
                start_line=line,
                service_id=service.id,
            )
            return

        shorts = _all_base_shorts(cls, converter)
        app_tag = cls.root().name.split(".")[0]
        if shorts & _VIEWSET_BASES or shorts & set(_VIEWSET_BASE_ACTIONS):
            actions = as_view_actions or {}
            for method, action in sorted(actions.items(), key=lambda t: _HTTP_METHODS.index(t[0]) if t[0] in _HTTP_METHODS else 99):
                if method not in _HTTP_METHODS:
                    continue
                action_func = _find_method(cls, converter, action)
                handler = f"{cls.root().name}.{cls.name}.{action}"
                routes.append(
                    RouteRef(
                        service_hint=service.id,
                        raw_path=raw,
                        methods=[method],
                        handler_symbol=handler,
                        file=rel,
                        start_line=line,
                        kind="viewset_action",
                    )
                )
                state.route_details[(handler, raw)] = {
                    "type": "viewset",
                    "cls": cls,
                    "action": action,
                    "func": action_func,
                    "path": template,
                    "params": params,
                    "tags": [app_tag],
                    "detail": bool(params),
                }
            if not actions:
                ctx.warnings.emit(
                    "W202",
                    "ViewSet used with as_view() but no action mapping literal found",
                    file=rel,
                    start_line=line,
                    service_id=service.id,
                )
            return

        generic_mapping: dict[str, str] = {}
        for short in shorts:
            generic_mapping.update(_GENERIC_VIEW_ACTIONS.get(short, {}))
        if generic_mapping:
            handler = f"{cls.root().name}.{cls.name}"
            methods = [m for m in _HTTP_METHODS if m in generic_mapping]
            routes.append(
                RouteRef(
                    service_hint=service.id,
                    raw_path=raw,
                    methods=methods,
                    handler_symbol=handler,
                    file=rel,
                    start_line=line,
                    kind="method_view",
                )
            )
            state.route_details[(handler, raw)] = {
                "type": "generic",
                "cls": cls,
                "mapping": generic_mapping,
                "path": template,
                "params": params,
                "tags": [app_tag],
                "detail": bool(params),
            }
            return

        # APIView / plain class-based view: explicitly defined verb methods
        methods = [m for m in _HTTP_METHODS if _find_method(cls, converter, m) is not None]
        if not methods:
            ctx.warnings.emit(
                "W202",
                f"class view {cls.name} defines no HTTP handler methods; skipped",
                file=rel,
                start_line=line,
                service_id=service.id,
            )
            return
        handler = f"{cls.root().name}.{cls.name}"
        routes.append(
            RouteRef(
                service_hint=service.id,
                raw_path=raw,
                methods=methods,
                handler_symbol=handler,
                file=rel,
                start_line=line,
                kind="method_view",
            )
        )
        state.route_details[(handler, raw)] = {
            "type": "apiview",
            "cls": cls,
            "path": template,
            "params": params,
            "tags": [app_tag],
            "detail": bool(params),
        }

    def _register_viewset_routes(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        state: DjangoState,
        converter: PyTypeSchemaConverter,
        urls_rel: str,
        register_call: nodes.Call,
        viewset: nodes.ClassDef,
        reg_prefix: str,
        url_prefix: tuple[tuple[str, bool], ...],
        routes: list[RouteRef],
    ) -> None:
        line = register_call.lineno or 1
        views_module = viewset.root()
        if views_module.file:
            service.dependencies.append(
                DependencyEdge(
                    from_file=urls_rel, to_file=ctx.rel(views_module.file), kind="url_include"
                )
            )
        lookup = "pk"
        lookup_attr = _class_attr_value(viewset, converter, "lookup_url_kwarg") or _class_attr_value(
            viewset, converter, "lookup_field"
        )
        if lookup_attr is not None:
            literal = _lit_str(lookup_attr[0])
            if literal:
                lookup = literal
        pk_schema = self._pk_schema(viewset, converter)
        pk_confidence = Confidence(level="medium", reason_code="framework_default")

        collection_template, collection_raw, base_params = self._compose_path(
            ctx, service, urls_rel, line, url_prefix, f"{reg_prefix}/", False
        )
        detail_template = f"{collection_template}{{{lookup}}}/"
        detail_raw = f"{collection_raw}<{lookup}>/"
        detail_params = base_params + [(lookup, pk_schema, pk_confidence)]

        actions = self._viewset_actions(viewset, converter)
        for action in _ACTION_ORDER:
            if action not in actions:
                continue
            method, is_detail = _STANDARD_ACTIONS[action]
            template = detail_template if is_detail else collection_template
            raw = detail_raw if is_detail else collection_raw
            handler = f"{views_module.name}.{viewset.name}.{action}"
            routes.append(
                RouteRef(
                    service_hint=service.id,
                    raw_path=raw,
                    methods=[method],
                    handler_symbol=handler,
                    file=urls_rel,
                    start_line=line,
                    kind="viewset_action",
                )
            )
            state.route_details[(handler, raw)] = {
                "type": "viewset",
                "cls": viewset,
                "action": action,
                "func": actions[action],
                "path": template,
                "params": detail_params if is_detail else list(base_params),
                "tags": [reg_prefix],
                "detail": is_detail,
            }

        for func, methods, is_detail, url_path, decorator in self._custom_actions(viewset, converter):
            segment = f"{url_path}/"
            template = (detail_template if is_detail else collection_template) + segment
            raw = (detail_raw if is_detail else collection_raw) + segment
            handler = f"{views_module.name}.{viewset.name}.{func.name}"
            routes.append(
                RouteRef(
                    service_hint=service.id,
                    raw_path=raw,
                    methods=methods,
                    handler_symbol=handler,
                    file=urls_rel,
                    start_line=line,
                    kind="viewset_action",
                )
            )
            state.route_details[(handler, raw)] = {
                "type": "custom_action",
                "cls": viewset,
                "action": func.name,
                "func": func,
                "decorator": decorator,
                "path": template,
                "params": detail_params if is_detail else list(base_params),
                "tags": [reg_prefix],
                "detail": is_detail,
            }

    @staticmethod
    def _viewset_actions(
        cls: nodes.ClassDef, converter: PyTypeSchemaConverter
    ) -> dict[str, nodes.FunctionDef | None]:
        inherited: set[str] = set()
        for short in _all_base_shorts(cls, converter):
            inherited |= _VIEWSET_BASE_ACTIONS.get(short, set())
        explicit: dict[str, nodes.FunctionDef] = {}
        for klass in _repo_mro(cls, converter):
            for stmt in klass.body:
                if isinstance(stmt, nodes.FunctionDef) and stmt.name in _STANDARD_ACTIONS:
                    explicit.setdefault(stmt.name, stmt)
        available = inherited | set(explicit)
        return {a: explicit.get(a) for a in _ACTION_ORDER if a in available}

    @staticmethod
    def _custom_actions(
        cls: nodes.ClassDef, converter: PyTypeSchemaConverter
    ) -> list[tuple[nodes.FunctionDef, list[str], bool, str, nodes.Call]]:
        results = []
        for klass in _repo_mro(cls, converter):
            for stmt in klass.body:
                if not isinstance(stmt, nodes.FunctionDef) or not stmt.decorators:
                    continue
                for decorator in stmt.decorators.nodes:
                    if not isinstance(decorator, nodes.Call):
                        continue
                    if _short(dotted_name(decorator.func)) != "action":
                        continue
                    kwargs = _kwargs_of(decorator)
                    detail = _lit_bool(kwargs.get("detail"))
                    methods = [m.lower() for m in _lit_str_list(kwargs.get("methods"))] or ["get"]
                    methods = [m for m in _HTTP_METHODS if m in methods]
                    url_path = _lit_str(kwargs.get("url_path")) or stmt.name
                    results.append((stmt, methods, detail, url_path, decorator))
                    break
        return results

    def _pk_schema(self, viewset: nodes.ClassDef, converter: PyTypeSchemaConverter) -> dict:
        model = self._model_of_view(viewset, converter)
        if model is not None:
            for klass in _repo_mro(model, converter):
                for stmt in klass.body:
                    if not isinstance(stmt, nodes.Assign) or not isinstance(stmt.value, nodes.Call):
                        continue
                    kwargs = _kwargs_of(stmt.value)
                    if _lit_bool(kwargs.get("primary_key")):
                        short = _short(dotted_name(stmt.value.func))
                        if short in _MODEL_FIELD_TYPES:
                            return dict(_MODEL_FIELD_TYPES[short])
        return {"type": "integer"}  # Django's default AutoField/BigAutoField pk

    def _model_of_view(
        self, cls: nodes.ClassDef, converter: PyTypeSchemaConverter
    ) -> nodes.ClassDef | None:
        queryset = _class_attr_value(cls, converter, "queryset")
        if queryset is not None:
            root = _root_name(queryset[0])
            if root:
                resolved = converter.resolve_symbol(cls.root(), root)
                if isinstance(resolved, nodes.ClassDef):
                    return resolved
        serializer = self._serializer_class_of(cls, converter)
        if serializer is not None:
            for klass in _repo_mro(serializer, converter):
                for stmt in klass.body:
                    if isinstance(stmt, nodes.ClassDef) and stmt.name == "Meta":
                        for meta_stmt in stmt.body:
                            if (
                                isinstance(meta_stmt, nodes.Assign)
                                and len(meta_stmt.targets) == 1
                                and isinstance(meta_stmt.targets[0], nodes.AssignName)
                                and meta_stmt.targets[0].name == "model"
                            ):
                                name = dotted_name(meta_stmt.value)
                                if name:
                                    resolved = converter.resolve_symbol(stmt.root(), name)
                                    if isinstance(resolved, nodes.ClassDef):
                                        return resolved
        return None

    @staticmethod
    def _serializer_class_of(
        cls: nodes.ClassDef, converter: PyTypeSchemaConverter
    ) -> nodes.ClassDef | None:
        found = _class_attr_value(cls, converter, "serializer_class")
        if found is None:
            return None
        name = dotted_name(found[0])
        if not name:
            return None
        resolved = converter.resolve_symbol(found[1].root(), name)
        return resolved if isinstance(resolved, nodes.ClassDef) else None

    @staticmethod
    def _function_view_methods(func: nodes.FunctionDef) -> list[str]:
        methods: list[str] = []
        for decorator in func.decorators.nodes if func.decorators else []:
            call = decorator if isinstance(decorator, nodes.Call) else None
            name = dotted_name(call.func if call else decorator) or ""
            short = _short(name)
            if short == "require_http_methods" and call is not None and call.args:
                methods.extend(m.lower() for m in _lit_str_list(call.args[0]))
            elif short == "require_POST":
                methods.append("post")
            elif short in ("require_GET", "require_safe"):
                methods.append("get")
        if not methods:
            for compare in func.nodes_of_class(nodes.Compare):
                if (dotted_name(compare.left) or "") != "request.method":
                    continue
                for op, operand in compare.ops:
                    if op == "==":
                        value = _lit_str(operand)
                        if value:
                            methods.append(value.lower())
                    elif op == "in":
                        methods.extend(m.lower() for m in _lit_str_list(operand))
        if not methods:
            methods = ["get"]
        seen = set(methods)
        return [m for m in _HTTP_METHODS if m in seen]

    # ------------------------------------------------------------ extraction
    def extract_operation(
        self, ctx: AnalysisContext, service: Service, route: RouteRef
    ) -> list[OperationExtraction]:
        assert isinstance(ctx, PythonAnalysisContext)
        state: DjangoState = ctx.extras["django_states"][service.id]
        converter, ser_conv = ctx.extras["django_tools"][service.id]
        details = state.route_details.get((route.handler_symbol, route.raw_path))
        if details is None:
            return []
        kind = details["type"]
        extractions: list[OperationExtraction] = []
        for method in route.methods:
            if kind == "function":
                extraction = self._extract_function(
                    ctx, service, converter, ser_conv, route, details, method
                )
            elif kind == "viewset":
                extraction = self._extract_drf(
                    ctx, service, state, converter, ser_conv, route, details, method,
                    action=details["action"], func=details["func"],
                )
            elif kind == "generic":
                action = details["mapping"].get(method)
                if action is None:
                    continue
                func = _find_method(details["cls"], converter, action)
                extraction = self._extract_drf(
                    ctx, service, state, converter, ser_conv, route, details, method,
                    action=action, func=func,
                )
            elif kind == "custom_action":
                extraction = self._extract_drf(
                    ctx, service, state, converter, ser_conv, route, details, method,
                    action=None, func=details["func"], decorator=details.get("decorator"),
                )
            else:  # apiview
                func = _find_method(details["cls"], converter, method)
                if func is None:
                    continue
                extraction = self._extract_drf(
                    ctx, service, state, converter, ser_conv, route, details, method,
                    action=None, func=func,
                )
            if extraction is not None:
                extractions.append(extraction)
        return extractions

    # -- function views ----------------------------------------------------------
    def _extract_function(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        converter: PyTypeSchemaConverter,
        ser_conv: DrfSerializerConverter,
        route: RouteRef,
        details: dict,
        method: str,
    ) -> OperationExtraction:
        func: nodes.FunctionDef = details["func"]
        normalized = details["path"]
        url_evidence = Evidence(
            file=route.file,
            start_line=route.start_line,
            end_line=route.start_line,
            kind="url_conf",
            symbol=route.handler_symbol,
        )
        module = func.root()
        func_rel = ctx.rel(module.file) if module.file else "unknown"
        def_evidence = Evidence(
            file=func_rel,
            start_line=func.lineno or 1,
            end_line=func.end_lineno or func.lineno or 1,
            kind="signature",
            symbol=route.handler_symbol,
        )
        unresolved: list[UnresolvedSite] = []
        scan = self._scan_view_method(ctx, func, None, None, converter, ser_conv)
        responses = self._variants_from_scan(
            service, scan, normalized, method, def_evidence, unresolved
        )
        summary, description = _doc_hints(func)
        parameters = self._parameters(details, url_evidence)
        operation = Operation(
            method=method,  # type: ignore[arg-type]
            operation_id=f"{service.id}.{route.handler_symbol}.{method}",
            handler=route.handler_symbol,
            parameters=parameters,
            request_body=None,
            responses=responses,
            security=[],
            tags_hint=[t for t in details.get("tags", []) if t],
            summary_hint=summary,
            description_hint=description,
            evidence=[url_evidence, def_evidence],
            confidence=Confidence(level="high", reason_code="declared_annotation"),
        )
        return OperationExtraction(
            endpoint_path=normalized,
            raw_path=route.raw_path,
            operation=operation,
            unresolved=unresolved,
        )

    # -- DRF class-based views -----------------------------------------------------
    def _extract_drf(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        state: DjangoState,
        converter: PyTypeSchemaConverter,
        ser_conv: DrfSerializerConverter,
        route: RouteRef,
        details: dict,
        method: str,
        action: str | None,
        func: nodes.FunctionDef | None,
        decorator: nodes.Call | None = None,
    ) -> OperationExtraction:
        cls: nodes.ClassDef = details["cls"]
        module = cls.root()
        views_rel = ctx.rel(module.file) if module.file else "unknown"
        normalized = details["path"]
        url_evidence = Evidence(
            file=route.file,
            start_line=route.start_line,
            end_line=route.start_line,
            kind="url_conf",
            symbol=route.handler_symbol,
        )
        def_node = func or cls
        def_evidence = Evidence(
            file=views_rel,
            start_line=def_node.lineno or 1,
            end_line=def_node.end_lineno or def_node.lineno or 1,
            kind="signature" if func is not None else "class_def",
            symbol=route.handler_symbol,
        )
        unresolved: list[UnresolvedSite] = []
        serializer_cls = self._serializer_class_of(cls, converter)

        if action in _STANDARD_ACTIONS:
            request_body, responses = self._standard_action_contract(
                ctx, service, state, converter, ser_conv, route, details, method,
                action, cls, serializer_cls, def_evidence, normalized, unresolved,
            )
        else:
            request_body, responses = self._scanned_method_contract(
                ctx, service, converter, ser_conv, route, method,
                cls, func, serializer_cls, def_evidence, normalized, unresolved,
            )

        security = self._security_for_class(ctx, state, service, cls, converter, decorator)
        parameters = self._parameters(details, url_evidence)

        if func is not None:
            summary, description = _doc_hints(func)
            op_confidence = Confidence(level="high", reason_code="declared_annotation")
        else:
            class_summary, _class_desc = _doc_hints(cls)
            summary, description = None, class_summary
            op_confidence = Confidence(level="medium", reason_code="framework_default")

        operation = Operation(
            method=method,  # type: ignore[arg-type]
            operation_id=f"{service.id}.{route.handler_symbol}.{method}",
            handler=route.handler_symbol,
            parameters=parameters,
            request_body=request_body,
            responses=responses,
            security=security,
            tags_hint=[t for t in details.get("tags", []) if t],
            summary_hint=summary,
            description_hint=description,
            evidence=[url_evidence, def_evidence],
            confidence=op_confidence,
        )
        return OperationExtraction(
            endpoint_path=normalized,
            raw_path=route.raw_path,
            operation=operation,
            unresolved=unresolved,
        )

    def _standard_action_contract(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        state: DjangoState,
        converter: PyTypeSchemaConverter,
        ser_conv: DrfSerializerConverter,
        route: RouteRef,
        details: dict,
        method: str,
        action: str,
        cls: nodes.ClassDef,
        serializer_cls: nodes.ClassDef | None,
        def_evidence: Evidence,
        normalized: str,
        unresolved: list[UnresolvedSite],
    ) -> tuple[RequestBody | None, list[ResponseVariant]]:
        ser_info = ser_conv.convert(serializer_cls) if serializer_cls is not None else None
        variants: list[ResponseVariant] = []
        counter: dict[str, int] = {}

        def add(variant: ResponseVariant) -> None:
            variant.variant_index = counter.get(variant.status, 0)
            counter[variant.status] = variant.variant_index + 1
            variants.append(variant)

        success_status = "201" if action == "create" else ("204" if action == "destroy" else "200")
        if action == "destroy":
            add(
                ResponseVariant(
                    status="204",
                    origin="framework_default",
                    content={},
                    evidence=[def_evidence],
                    confidence=Confidence(level="medium", reason_code="framework_default"),
                )
            )
        else:
            if ser_info is not None:
                base = ser_info.ref_schema
                confidence = _worse(
                    ser_info.confidence,
                    Confidence(level="medium", reason_code="inferred_serializer"),
                )
                if action == "list":
                    if self._is_paginated(cls, state, converter):
                        schema = {
                            "type": "object",
                            "properties": {
                                "count": {"type": "integer"},
                                "next": {"type": ["string", "null"], "format": "uri"},
                                "previous": {"type": ["string", "null"], "format": "uri"},
                                "results": {"type": "array", "items": base},
                            },
                            "required": ["count", "results"],
                        }
                    else:
                        schema = {"type": "array", "items": base}
                else:
                    schema = base
                if ser_info.unresolved_fields:
                    unresolved.append(
                        UnresolvedSite(
                            service_id=service.id,
                            path=normalized,
                            method=method,
                            site=f"responses/{success_status}/content/application~1json/schema",
                            kind="response_schema",
                            reason_code="dynamic_type",
                            evidence=[def_evidence],
                        )
                    )
            else:
                schema = {}
                confidence = Confidence(level="low", reason_code="unresolved_symbol")
                unresolved.append(
                    UnresolvedSite(
                        service_id=service.id,
                        path=normalized,
                        method=method,
                        site=f"responses/{success_status}/content/application~1json/schema",
                        kind="response_schema",
                        reason_code="unresolved_symbol",
                        evidence=[def_evidence],
                    )
                )
            add(
                ResponseVariant(
                    status=success_status,
                    origin="serializer",
                    content={"application/json": MediaTypeContract(schema=schema)},
                    evidence=[def_evidence],
                    confidence=confidence,
                )
            )

        if action in ("create", "update", "partial_update"):
            add(self._validation_variant(def_evidence))
        if details.get("detail") and action != "create":
            add(self._not_found_variant(def_evidence))

        request_body: RequestBody | None = None
        if action in ("create", "update", "partial_update"):
            body_evidence = [def_evidence]
            attr = _class_attr_value(cls, converter, "serializer_class")
            if attr is not None:
                owner_module = attr[1].root()
                body_evidence.append(
                    Evidence(
                        file=ctx.rel(owner_module.file) if owner_module.file else "unknown",
                        start_line=attr[2],
                        end_line=attr[3],
                        kind="assignment",
                        symbol=f"{attr[1].qname()}.serializer_class",
                    )
                )
            if ser_info is not None:
                body_info = ser_conv.convert(serializer_cls, partial=action == "partial_update")
                request_body = RequestBody(
                    required=action != "partial_update",
                    content={"application/json": MediaTypeContract(schema=body_info.ref_schema)},
                    evidence=body_evidence,
                    confidence=_worse(
                        body_info.confidence,
                        Confidence(level="medium", reason_code="inferred_serializer"),
                    ),
                )
            else:
                request_body = RequestBody(
                    required=action != "partial_update",
                    content={"application/json": MediaTypeContract(schema={})},
                    evidence=body_evidence,
                    confidence=Confidence(level="low", reason_code="unresolved_symbol"),
                )
                unresolved.append(
                    UnresolvedSite(
                        service_id=service.id,
                        path=normalized,
                        method=method,
                        site="request_body/content/application~1json/schema",
                        kind="request_schema",
                        reason_code="unresolved_symbol",
                        evidence=[def_evidence],
                    )
                )
        return request_body, variants

    def _scanned_method_contract(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        converter: PyTypeSchemaConverter,
        ser_conv: DrfSerializerConverter,
        route: RouteRef,
        method: str,
        cls: nodes.ClassDef,
        func: nodes.FunctionDef | None,
        serializer_cls: nodes.ClassDef | None,
        def_evidence: Evidence,
        normalized: str,
        unresolved: list[UnresolvedSite],
    ) -> tuple[RequestBody | None, list[ResponseVariant]]:
        scan = (
            self._scan_view_method(ctx, func, cls, serializer_cls, converter, ser_conv)
            if func is not None
            else MethodScan()
        )
        variants = self._variants_from_scan(
            service, scan, normalized, method, def_evidence, unresolved
        )
        if scan.has_validation:
            variant = self._validation_variant(def_evidence)
            variant.variant_index = sum(1 for v in variants if v.status == "400")
            variants.append(variant)
        if scan.has_get_object:
            variant = self._not_found_variant(def_evidence)
            variant.variant_index = sum(1 for v in variants if v.status == "404")
            variants.append(variant)

        request_body: RequestBody | None = None
        if method in ("post", "put", "patch") and scan.request_serializer is not None:
            info = ser_conv.convert(scan.request_serializer, partial=scan.request_partial)
            request_body = RequestBody(
                required=not scan.request_partial,
                content={"application/json": MediaTypeContract(schema=info.ref_schema)},
                evidence=[def_evidence],
                confidence=_worse(
                    info.confidence, Confidence(level="medium", reason_code="inferred_serializer")
                ),
            )
        return request_body, variants

    def _variants_from_scan(
        self,
        service: Service,
        scan: MethodScan,
        normalized: str,
        method: str,
        def_evidence: Evidence,
        unresolved: list[UnresolvedSite],
    ) -> list[ResponseVariant]:
        by_status: dict[str, tuple] = {}
        status_order: list[str] = []
        for status, schema, origin, confidence, evidence in scan.returns:
            if status not in by_status:
                status_order.append(status)
                by_status[status] = (schema, origin, confidence, evidence)
            elif by_status[status][0] is None and schema is not None:
                by_status[status] = (schema, origin, confidence, evidence)
        variants: list[ResponseVariant] = []
        if not by_status:
            unresolved.append(
                UnresolvedSite(
                    service_id=service.id,
                    path=normalized,
                    method=method,
                    site="responses/200/content/application~1json/schema",
                    kind="response_schema",
                    reason_code="dynamic_type",
                    evidence=[def_evidence],
                )
            )
            variants.append(
                ResponseVariant(
                    status="200",
                    origin="framework_default",
                    content={"application/json": MediaTypeContract(schema={})},
                    evidence=[def_evidence],
                    confidence=Confidence(level="low", reason_code="dynamic_type"),
                )
            )
            return variants
        for status in status_order:
            schema, origin, confidence, evidence = by_status[status]
            if status == "204":
                variants.append(
                    ResponseVariant(
                        status=status,
                        origin=origin,
                        content={},
                        evidence=[evidence],
                        confidence=confidence
                        or Confidence(level="high", reason_code="declared_annotation"),
                    )
                )
                continue
            if schema is None:
                unresolved.append(
                    UnresolvedSite(
                        service_id=service.id,
                        path=normalized,
                        method=method,
                        site=f"responses/{status}/content/application~1json/schema",
                        kind="response_schema",
                        reason_code="dynamic_type",
                        evidence=[evidence],
                    )
                )
                schema = {}
                confidence = Confidence(level="low", reason_code="dynamic_type")
            variants.append(
                ResponseVariant(
                    status=status,
                    origin=origin,
                    content={"application/json": MediaTypeContract(schema=schema)},
                    evidence=[evidence],
                    confidence=confidence
                    or Confidence(level="medium", reason_code="inferred_return_flow"),
                )
            )
        return variants

    def _scan_view_method(
        self,
        ctx: PythonAnalysisContext,
        func: nodes.FunctionDef,
        cls: nodes.ClassDef | None,
        serializer_cls: nodes.ClassDef | None,
        converter: PyTypeSchemaConverter,
        ser_conv: DrfSerializerConverter,
    ) -> MethodScan:
        scan = MethodScan()
        module = func.root()
        rel = ctx.rel(module.file) if module.file else "unknown"

        def serializer_from_call(call: nodes.Call) -> nodes.ClassDef | None:
            func_name = dotted_name(call.func) or ""
            if _short(func_name) == "get_serializer" and serializer_cls is not None:
                return serializer_cls
            try:
                resolved = converter.resolve_symbol(module, func_name)
            except Exception:  # noqa: BLE001
                return None
            if isinstance(resolved, nodes.ClassDef) and _is_serializer_class(resolved, converter):
                return resolved
            return None

        var_serializers: dict[str, tuple[nodes.ClassDef, bool]] = {}
        for assign in func.nodes_of_class(nodes.Assign):
            if not isinstance(assign.value, nodes.Call) or len(assign.targets) != 1:
                continue
            target = assign.targets[0]
            if not isinstance(target, nodes.AssignName):
                continue
            resolved = serializer_from_call(assign.value)
            if resolved is None:
                continue
            kwargs = _kwargs_of(assign.value)
            var_serializers[target.name] = (resolved, _lit_bool(kwargs.get("many")))
            data = kwargs.get("data")
            if data is not None and (dotted_name(data) or "").startswith("request."):
                scan.request_serializer = resolved
                scan.request_partial = _lit_bool(kwargs.get("partial"))

        for call in func.nodes_of_class(nodes.Call):
            callee = call.func
            if isinstance(callee, nodes.Attribute):
                if callee.attrname == "is_valid":
                    if _lit_bool(_kwargs_of(call).get("raise_exception")):
                        scan.has_validation = True
                elif callee.attrname in ("get_object", "get_object_or_404"):
                    scan.has_get_object = True
            elif isinstance(callee, nodes.Name) and callee.name == "get_object_or_404":
                scan.has_get_object = True

        for ret in func.nodes_of_class(nodes.Return):
            if not isinstance(ret.value, nodes.Call):
                continue
            call = ret.value
            short = _short(dotted_name(call.func))
            if short not in ("Response", "JsonResponse", "HttpResponse", "HttpResponseNotFound"):
                continue
            kwargs = _kwargs_of(call)
            status = _status_from_node(kwargs.get("status"))
            if status is None:
                status = 404 if short == "HttpResponseNotFound" else 200
            evidence = Evidence(
                file=rel,
                start_line=ret.lineno or 1,
                end_line=ret.end_lineno or ret.lineno or 1,
                kind="return_stmt",
                symbol=None,
            )
            schema: dict | None = None
            confidence: Confidence | None = None
            origin = "explicit_response_object"
            if short in ("Response", "JsonResponse") and call.args:
                arg = call.args[0]
                ok, value = literal_value(arg)
                if ok and isinstance(value, dict):
                    schema = _literal_schema(value)
                    confidence = Confidence(level="medium", reason_code="inferred_return_flow")
                elif isinstance(arg, nodes.Attribute) and arg.attrname in ("data", "validated_data"):
                    base = arg.expr
                    resolved_pair: tuple[nodes.ClassDef, bool] | None = None
                    if isinstance(base, nodes.Name) and base.name in var_serializers:
                        resolved_pair = var_serializers[base.name]
                    elif isinstance(base, nodes.Call):
                        resolved = serializer_from_call(base)
                        if resolved is not None:
                            resolved_pair = (resolved, _lit_bool(_kwargs_of(base).get("many")))
                    if resolved_pair is not None:
                        info = ser_conv.convert(resolved_pair[0])
                        schema = (
                            {"type": "array", "items": info.ref_schema}
                            if resolved_pair[1]
                            else info.ref_schema
                        )
                        confidence = _worse(
                            info.confidence,
                            Confidence(level="medium", reason_code="inferred_serializer"),
                        )
                        origin = "serializer"
            elif short == "HttpResponse" and status == 200 and not call.args:
                schema = None
            scan.returns.append((str(status), schema, origin, confidence, evidence))
        return scan

    # -- shared framework-default variants ------------------------------------
    @staticmethod
    def _validation_variant(evidence: Evidence) -> ResponseVariant:
        return ResponseVariant(
            status="400",
            origin="framework_default",
            condition=Condition(
                kind="exception_handled",
                exception_type="rest_framework.exceptions.ValidationError",
            ),
            content={"application/json": MediaTypeContract(schema={"type": "object"})},
            evidence=[evidence],
            confidence=Confidence(level="medium", reason_code="framework_default"),
        )

    @staticmethod
    def _not_found_variant(evidence: Evidence) -> ResponseVariant:
        return ResponseVariant(
            status="404",
            origin="exception_handler",
            condition=Condition(
                kind="exception_handled", exception_type="django.http.Http404"
            ),
            content={"application/json": MediaTypeContract(schema=dict(_NOT_FOUND_SCHEMA))},
            evidence=[evidence],
            confidence=Confidence(level="medium", reason_code="framework_default"),
        )

    @staticmethod
    def _parameters(details: dict, url_evidence: Evidence) -> list[Parameter]:
        parameters: list[Parameter] = []
        seen: set[str] = set()
        for name, schema, confidence in details.get("params", []):
            if name in seen:
                continue
            seen.add(name)
            parameters.append(
                Parameter(
                    name=name,
                    location="path",
                    required=True,
                    schema=dict(schema),
                    evidence=[url_evidence],
                    confidence=confidence,
                )
            )
        return parameters

    def _is_paginated(
        self, cls: nodes.ClassDef, state: DjangoState, converter: PyTypeSchemaConverter
    ) -> bool:
        attr = _class_attr_value(cls, converter, "pagination_class")
        if attr is not None:
            value = attr[0]
            if isinstance(value, nodes.Const) and value.value is None:
                return False
            return True
        return state.default_pagination

    def _security_for_class(
        self,
        ctx: PythonAnalysisContext,
        state: DjangoState,
        service: Service,
        cls: nodes.ClassDef,
        converter: PyTypeSchemaConverter,
        action_decorator: nodes.Call | None = None,
    ) -> list[SecurityEvidence]:
        perm_shorts: list[str] = []
        perm_evidence: Evidence | None = None
        decorator_kwargs = _kwargs_of(action_decorator) if action_decorator is not None else {}

        if "permission_classes" in decorator_kwargs:
            perm_shorts = _name_shorts(decorator_kwargs["permission_classes"])
            perm_evidence = Evidence(
                file=ctx.rel(cls.root().file) if cls.root().file else "unknown",
                start_line=action_decorator.lineno or 1,
                end_line=action_decorator.end_lineno or action_decorator.lineno or 1,
                kind="decorator",
                symbol=f"{cls.qname()}",
            )
        else:
            attr = _class_attr_value(cls, converter, "permission_classes")
            if attr is not None:
                value, owner, start, end = attr
                perm_shorts = _name_shorts(value)
                owner_module = owner.root()
                perm_evidence = Evidence(
                    file=ctx.rel(owner_module.file) if owner_module.file else "unknown",
                    start_line=start,
                    end_line=end,
                    kind="assignment",
                    symbol=f"{owner.qname()}.permission_classes",
                )
            else:
                perm_shorts = sorted(state.default_permissions)
                perm_evidence = state.settings_evidence

        if "AllowAny" in perm_shorts:
            return []
        if not set(perm_shorts) & _AUTH_REQUIRED_PERMISSIONS:
            return []

        auth_decls: list[AuthDecl] = []
        auth_attr = _class_attr_value(cls, converter, "authentication_classes")
        if auth_attr is not None:
            value, owner, start, end = auth_attr
            owner_module = owner.root()
            auth_evidence = Evidence(
                file=ctx.rel(owner_module.file) if owner_module.file else "unknown",
                start_line=start,
                end_line=end,
                kind="assignment",
                symbol=f"{owner.qname()}.authentication_classes",
            )
            for short in _name_shorts(value):
                if short in _AUTH_CLASSES:
                    scheme_id, kind, detail = _AUTH_CLASSES[short]
                    auth_decls.append(AuthDecl(scheme_id, kind, dict(detail), auth_evidence))
        else:
            auth_decls = state.default_auth

        result: list[SecurityEvidence] = []
        seen: set[str] = set()
        for decl in auth_decls:
            if decl.scheme_id in seen:
                continue
            seen.add(decl.scheme_id)
            if decl.scheme_id not in service.security_schemes:
                service.security_schemes[decl.scheme_id] = SecuritySchemeDecl(
                    scheme_id=decl.scheme_id,
                    kind=decl.kind,  # type: ignore[arg-type]
                    detail=dict(decl.detail),
                    evidence=[decl.evidence],
                )
            evidence = [decl.evidence]
            if perm_evidence is not None and perm_evidence != decl.evidence:
                evidence = [perm_evidence, decl.evidence]
            result.append(
                SecurityEvidence(
                    scheme_id=decl.scheme_id,
                    mechanism="permission_class",
                    evidence=evidence,
                    confidence=Confidence(level="high", reason_code="declared_annotation"),
                )
            )
        return result  # empty when auth requirement is unprovable: never guess
