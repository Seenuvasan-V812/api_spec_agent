"""Phase-1 metadata document models (the analyzer's output contract).

Design rules (normative):

- Language-neutral: nothing here is Python- or Java-specific except
  ``LangTypeRef`` which records *where a schema came from*.
- Embedded schemas are raw JSON Schema 2020-12 fragments (``JsonSchemaDict``);
  JSON Schema is not re-modeled field-by-field.
- Provenance (evidence + confidence) attaches at the contract-fact level:
  Operation, Parameter, RequestBody, ResponseVariant, HeaderSpec,
  SecurityEvidence, SchemaEntry.
- Deterministic: no timestamps/hostnames/usernames; repo-relative POSIX paths;
  :func:`canonicalize` sorts every collection before emit and
  :func:`to_canonical_json` produces byte-stable output.
- Never guess: unresolvable contracts are ``{}`` schemas with low confidence
  plus an :class:`UnresolvedContract` pointer — "probably" is unrepresentable.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

JsonSchemaDict = dict[str, Any]

HTTP_METHOD_ORDER: tuple[str, ...] = (
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
)

_PARAM_LOCATION_ORDER = {"path": 0, "query": 1, "header": 2, "cookie": 3}

ReasonCode = Literal[
    "declared_annotation",  # high: explicit decorator/annotation
    "declared_type",  # high: typed signature / DTO class
    "inferred_return_flow",  # medium: dataflow through return statements
    "inferred_serializer",  # medium: serializer/marshalling class resolved
    "dynamic_type",  # low: Any/Object/dict/JsonNode/**kwargs
    "unresolved_symbol",  # low: import or type could not be resolved
    "generated_code",  # low: codegen/Lombok indirection partially resolved
    "conditional_conflict",  # medium: multiple branches, all resolved
    "framework_default",  # medium: framework-documented default, not in source
    "parse_error_partial",  # low: file had syntax errors, partial AST
    "sidecar_unavailable",  # low: JVM sidecar missing, tree-sitter-only facts
]


def _normalize_repo_path(value: str) -> str:
    value = unicodedata.normalize("NFC", value).replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Evidence(_StrictModel):
    file: str  # repo-relative, POSIX separators
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    kind: Literal[
        "decorator",
        "annotation",
        "signature",
        "return_stmt",
        "raise_stmt",
        "class_def",
        "field_def",
        "call_site",
        "assignment",
        "config_file",
        "url_conf",
        "import",
        "exception_handler",
        "filter_chain",
    ]
    symbol: Optional[str] = None

    _norm_file = field_validator("file")(staticmethod(_normalize_repo_path))

    def sort_key(self) -> tuple[str, int, str]:
        return (self.file, self.start_line, self.kind)


class Confidence(_StrictModel):
    level: Literal["high", "medium", "low"]
    reason_code: ReasonCode
    detail: Optional[str] = None  # templated, deterministic sentence


class Provenance(_StrictModel):
    """Mixin carried by every contract-level fact."""

    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence


class Condition(_StrictModel):
    kind: Literal[
        "exception_handled",
        "branch",
        "early_return",
        "annotation",
        "default_success",
        "content_negotiation",
    ]
    expression: Optional[str] = None  # verbatim guard text
    exception_type: Optional[str] = None  # qualified exception name


class HeaderSpec(Provenance):
    name: str
    schema_: JsonSchemaDict = Field(default_factory=dict, alias="schema")
    required: Optional[bool] = None


class EncodingSpec(_StrictModel):
    content_type: Optional[str] = None
    headers: dict[str, HeaderSpec] = Field(default_factory=dict)


class MediaTypeContract(_StrictModel):
    schema_: JsonSchemaDict = Field(default_factory=dict, alias="schema")
    encoding: dict[str, EncodingSpec] = Field(default_factory=dict)
    example: Optional[Any] = None  # only when literally present in source


class Parameter(Provenance):
    name: str
    location: Literal["path", "query", "header", "cookie"]
    required: bool
    schema_: JsonSchemaDict = Field(default_factory=dict, alias="schema")
    description_hint: Optional[str] = None  # verbatim source text only
    style: Optional[str] = None
    explode: Optional[bool] = None
    deprecated: Optional[bool] = None
    default_repr: Optional[Any] = None  # JSON-safe rendering of literal default

    def sort_key(self) -> tuple[int, str]:
        return (_PARAM_LOCATION_ORDER[self.location], self.name)


class RequestBody(Provenance):
    required: bool = True
    content: dict[str, MediaTypeContract] = Field(default_factory=dict)
    description_hint: Optional[str] = None


class ResponseVariant(Provenance):
    status: str  # "200", "default", "4XX"
    variant_index: int = 0  # source-position ordered within the operation
    condition: Optional[Condition] = None
    origin: Literal[
        "annotation",
        "return_type",
        "raise_site",
        "exception_handler",
        "controller_advice",
        "serializer",
        "explicit_response_object",
        "framework_default",
    ]
    description_hint: Optional[str] = None
    content: dict[str, MediaTypeContract] = Field(default_factory=dict)
    headers: dict[str, HeaderSpec] = Field(default_factory=dict)

    def sort_key(self) -> tuple[int, str, int]:
        # numeric statuses first (ascending), then ranges/default
        try:
            rank, tag = int(self.status), ""
        except ValueError:
            rank, tag = 1000, self.status
        return (rank, tag, self.variant_index)


class SecuritySchemeDecl(_StrictModel):
    scheme_id: str
    kind: Literal[
        "http_bearer",
        "http_basic",
        "apikey_header",
        "apikey_query",
        "apikey_cookie",
        "oauth2",
        "openid_connect",
        "mutual_tls",
        "custom",
    ]
    detail: JsonSchemaDict = Field(default_factory=dict)  # only source-proven keys
    evidence: list[Evidence] = Field(default_factory=list)


class SecurityEvidence(Provenance):
    scheme_id: str
    scopes: list[str] = Field(default_factory=list)
    mechanism: Literal[
        "decorator",
        "dependency_injection",
        "annotation",
        "filter_chain_config",
        "middleware",
        "permission_class",
    ]


class Operation(Provenance):
    method: Literal["get", "put", "post", "delete", "options", "head", "patch", "trace"]
    operation_id: str  # deterministic: {service_id}.{handler_qualified_name}.{method}
    handler: str
    parameters: list[Parameter] = Field(default_factory=list)
    request_body: Optional[RequestBody] = None
    responses: list[ResponseVariant] = Field(default_factory=list)
    security: list[SecurityEvidence] = Field(default_factory=list)  # empty == unproven
    deprecated: Optional[bool] = None
    tags_hint: list[str] = Field(default_factory=list)  # verbatim from source only
    summary_hint: Optional[str] = None  # docstring first line / annotation summary
    description_hint: Optional[str] = None  # docstring body


class Endpoint(_StrictModel):
    path: str  # normalized OpenAPI template: /users/{user_id}
    raw_path: str  # framework-native: /users/<int:user_id>
    operations: list[Operation] = Field(default_factory=list)


class DependencyEdge(_StrictModel):
    from_file: str
    to_file: str
    kind: Literal[
        "import",
        "include_router",
        "url_include",
        "component_scan",
        "call",
        "sidecar_resolved",
    ]

    _norm_from = field_validator("from_file")(staticmethod(_normalize_repo_path))
    _norm_to = field_validator("to_file")(staticmethod(_normalize_repo_path))


class Service(_StrictModel):
    id: str
    name: str
    language: Literal["python", "java"]
    framework: str  # adapter name: fastapi | flask | django | drf | spring-mvc | ...
    framework_version: Optional[str] = None
    build_system: Optional[str] = None  # pip | poetry | maven | gradle | ...
    root_path: str = ""  # repo-relative dir of the service
    base_paths: list[str] = Field(default_factory=list)
    description_hint: Optional[str] = None
    security_schemes: dict[str, SecuritySchemeDecl] = Field(default_factory=dict)
    endpoints: list[Endpoint] = Field(default_factory=list)
    dependencies: list[DependencyEdge] = Field(default_factory=list)

    _norm_root = field_validator("root_path")(staticmethod(_normalize_repo_path))


class LangTypeRef(_StrictModel):
    language: Literal["python", "java"]
    qualified_name: str
    type_args: list["LangTypeRef"] = Field(default_factory=list)


class SchemaEntry(Provenance):
    schema_id: str
    title: str  # short name used for Phase-2 component naming
    lang_type: Optional[LangTypeRef] = None
    structural_hash: str
    json_schema: JsonSchemaDict = Field(default_factory=dict)
    used_by_services: list[str] = Field(default_factory=list)


class SchemaRegistry(_StrictModel):
    schemas: dict[str, SchemaEntry] = Field(default_factory=dict)


class AnalysisWarning(_StrictModel):
    code: str  # closed vocabulary, e.g. "W101"
    message: str  # templated, deterministic
    file: Optional[str] = None
    start_line: Optional[int] = None
    service_id: Optional[str] = None

    @field_validator("file")
    @classmethod
    def _norm_file(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _normalize_repo_path(v)

    def sort_key(self) -> tuple[str, str, int, str]:
        return (self.code, self.file or "", self.start_line or 0, self.message)


class UnresolvedContract(_StrictModel):
    pointer: str  # JSON Pointer into THIS document
    kind: Literal[
        "response_schema",
        "request_schema",
        "parameter_type",
        "status_code",
        "media_type",
        "security",
        "route_path",
    ]
    reason_code: ReasonCode
    evidence: list[Evidence] = Field(default_factory=list)


class CoverageMetrics(_StrictModel):
    files_scanned: int = 0
    files_failed_parse: int = 0
    endpoints_total: int = 0
    operations_total: int = 0
    operations_with_request_schema: int = 0
    operations_needing_body: int = 0
    responses_total: int = 0
    responses_with_schema: int = 0
    parameters_total: int = 0
    parameters_typed: int = 0
    operations_with_security: int = 0
    confidence_counts: dict[str, int] = Field(default_factory=dict)
    unresolved_count: int = 0


class GeneratorInfo(_StrictModel):
    tool: str = "openapi-agent"
    tool_version: str
    # deliberately no timestamp / hostname / username


class RepoInfo(_StrictModel):
    vcs_commit: Optional[str] = None  # the only run-context field
    languages: list[str] = Field(default_factory=list)
    build_systems: list[str] = Field(default_factory=list)
    analyzed_roots: list[str] = Field(default_factory=list)


class MetadataDocument(_StrictModel):
    metadata_version: str
    schema_uri: str = "https://openapi-agent.dev/schemas/metadata-v1.schema.json"
    generator: GeneratorInfo
    repo: RepoInfo
    services: list[Service] = Field(default_factory=list)
    schema_registry: SchemaRegistry = Field(default_factory=SchemaRegistry)
    warnings: list[AnalysisWarning] = Field(default_factory=list)
    unresolved: list[UnresolvedContract] = Field(default_factory=list)
    coverage: CoverageMetrics = Field(default_factory=CoverageMetrics)


# ---------------------------------------------------------------------------
# Canonicalization & serialization
# ---------------------------------------------------------------------------


def _method_rank(method: str) -> int:
    try:
        return HTTP_METHOD_ORDER.index(method)
    except ValueError:
        return len(HTTP_METHOD_ORDER)


def canonicalize(document: MetadataDocument) -> MetadataDocument:
    """Sort every collection by its documented key (in place; returns doc).

    Dict key ordering is handled at serialization time (``sort_keys=True``);
    this pass fixes list orderings so output never depends on discovery order.
    """
    for service in document.services:
        service.base_paths.sort()
        service.dependencies.sort(key=lambda d: (d.from_file, d.to_file, d.kind))
        service.endpoints.sort(key=lambda e: e.path)
        for endpoint in service.endpoints:
            endpoint.operations.sort(key=lambda o: _method_rank(o.method))
            for operation in endpoint.operations:
                operation.parameters.sort(key=lambda p: p.sort_key())
                operation.responses.sort(key=lambda r: r.sort_key())
                operation.security.sort(key=lambda s: (s.scheme_id, s.mechanism))
                operation.evidence.sort(key=lambda e: e.sort_key())
                for param in operation.parameters:
                    param.evidence.sort(key=lambda e: e.sort_key())
                if operation.request_body is not None:
                    operation.request_body.evidence.sort(key=lambda e: e.sort_key())
                for response in operation.responses:
                    response.evidence.sort(key=lambda e: e.sort_key())
                for sec in operation.security:
                    sec.scopes.sort()
                    sec.evidence.sort(key=lambda e: e.sort_key())
    document.services.sort(key=lambda s: s.id)
    for entry in document.schema_registry.schemas.values():
        entry.used_by_services.sort()
        entry.evidence.sort(key=lambda e: e.sort_key())
    document.repo.languages.sort()
    document.repo.build_systems.sort()
    document.repo.analyzed_roots.sort()
    document.warnings.sort(key=lambda w: w.sort_key())
    document.unresolved.sort(key=lambda u: u.pointer)
    return document


def to_canonical_json(document: MetadataDocument) -> str:
    """Byte-stable serialization: same input + tool version => identical bytes."""
    canonicalize(document)
    payload = document.model_dump(by_alias=True, exclude_none=True, mode="json")
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


def export_metadata_schema() -> dict[str, Any]:
    """Published JSON Schema for the metadata document (2020-12 dialect)."""
    schema = MetadataDocument.model_json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://openapi-agent.dev/schemas/metadata-v1.schema.json"
    return schema
