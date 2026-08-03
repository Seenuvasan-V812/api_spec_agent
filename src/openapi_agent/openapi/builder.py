"""Deterministic OpenAPI 3.1 assembly from Phase-1 metadata.

The document structure (paths, methods, parameters, schemas, statuses,
security) comes exclusively from the metadata document. The pluggable
``enricher`` may only contribute description-class text (summaries,
descriptions, tags); its output is grounded and validated elsewhere and never
adds or alters contract facts. Internal metadata (evidence, confidence,
call graphs) never reaches the emitted document.

Generation-time policies (never invent contract facts, only shape/convention):
- service-internal paths (``/internal``, ``/actuator``, …) are excluded from
  the public document;
- the base path lives in exactly one place — a server-URL path that duplicates
  the common path prefix is dropped so paths are not double-prefixed;
- ``operationId``s are clean, stable, path-derived (no package/class names);
- empty ``{}`` schemas are never emitted (typed no-content or a free-form
  object fallback instead);
- error responses reference one shared error schema;
- conventional responses (401/403/404/default) are added by method + auth;
- ``pattern`` values are anchored.
"""

from __future__ import annotations

import copy
import re
from http.client import responses as _HTTP_PHRASES
from typing import Any

from openapi_agent.config.loader import AgentConfig
from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import (
    Endpoint,
    MetadataDocument,
    Operation,
    ResponseVariant,
    SecuritySchemeDecl,
    Service,
)
from openapi_agent.openapi.components import ComponentsRegistry

log = get_logger("openapi.builder")

OPENAPI_VERSION = "3.1.0"

_FREEFORM_OBJECT: dict[str, Any] = {"type": "object", "additionalProperties": True}


# ---------------------------------------------------------------------------
# public helpers (shared with validators / report / generator)
# ---------------------------------------------------------------------------


def is_internal_path(path: str, prefixes: list[str]) -> bool:
    for prefix in prefixes:
        prefix = prefix.rstrip("/")
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            return True
    return False


def public_endpoints(service: Service, config: AgentConfig) -> list[Endpoint]:
    """Endpoints that belong in the public spec (internal paths removed)."""
    prefixes = config.openapi.internal_path_prefixes
    return [ep for ep in service.endpoints if not is_internal_path(ep.path, prefixes)]


def status_phrase(status: str) -> str:
    if status == "default":
        return "Unexpected error"
    if status.endswith("XX") and len(status) == 3:
        return {"2": "Success", "3": "Redirection", "4": "Client error", "5": "Server error"}.get(
            status[0], "Response"
        )
    try:
        return _HTTP_PHRASES.get(int(status), "Response")
    except ValueError:
        return "Response"


# ---------------------------------------------------------------------------
# document assembly
# ---------------------------------------------------------------------------


def build_openapi_document(
    document: MetadataDocument,
    service: Service,
    config: AgentConfig,
    enricher,
) -> dict[str, Any]:
    """Assemble the OpenAPI dict for one service."""
    components = ComponentsRegistry(document)
    multi_service = len(document.services) > 1
    title = config.openapi.title if not multi_service else f"{config.openapi.title} - {service.name}"

    endpoints = public_endpoints(service, config)
    all_paths = [ep.path for ep in endpoints]

    info: dict[str, Any] = {"title": title, "version": config.openapi.version}
    overview = enricher.api_overview(service)
    if overview:
        info["description"] = overview
    if config.openapi.contact:
        info["contact"] = dict(config.openapi.contact)
    if config.openapi.license:
        info["license"] = dict(config.openapi.license)

    servers = _build_servers(config, all_paths)

    error_schema = _service_error_schema(endpoints, components)
    seen_operation_ids: set[str] = set()

    paths: dict[str, Any] = {}
    inline_fragments: list[dict] = []
    tag_names: dict[str, None] = {}

    for endpoint in endpoints:
        path_item: dict[str, Any] = {}
        for operation in endpoint.operations:
            op_obj = _build_operation(
                operation, endpoint.path, service, components, enricher,
                inline_fragments, seen_operation_ids, error_schema, config,
            )
            for tag in op_obj.get("tags", []):
                tag_names.setdefault(tag, None)
            path_item[operation.method] = op_obj
        if path_item:
            paths[endpoint.path] = path_item

    security_schemes = _build_security_schemes(service)

    doc: dict[str, Any] = {
        "openapi": OPENAPI_VERSION,
        "info": info,
        "servers": servers,
        "paths": paths,
    }
    if tag_names:
        doc["tags"] = [_tag_object(name, enricher, service) for name in sorted(tag_names)]

    components_schemas = components.reachable_components(inline_fragments)
    components_obj: dict[str, Any] = {}
    if components_schemas:
        components_obj["schemas"] = components_schemas
    if security_schemes:
        components_obj["securitySchemes"] = security_schemes
    if components_obj:
        doc["components"] = components_obj

    _anchor_patterns(doc)
    return doc


def _build_servers(config: AgentConfig, paths: list[str]) -> list[dict[str, Any]]:
    """Server objects with the base path expressed once.

    If a server URL's path component duplicates the common prefix of every
    path, drop it from the server URL (the base path then lives only in the
    document paths). OpenAPI server variables are passed through.
    """
    common = _common_path_prefix(paths)
    servers: list[dict[str, Any]] = []
    for entry in config.openapi.servers:
        url = entry.url
        server_path = _url_path(url)
        if server_path and common and _prefix_matches(server_path, common):
            url = url[: len(url) - len(server_path)] or "/"
        obj: dict[str, Any] = {"url": url}
        if entry.description:
            obj["description"] = entry.description
        if entry.variables:
            obj["variables"] = entry.variables
        servers.append(obj)
    return servers


def _tag_object(name: str, enricher, service: Service) -> dict[str, Any]:
    tag: dict[str, Any] = {"name": name}
    description = enricher.tag_description(name, service)
    if description:
        tag["description"] = description
    return tag


def _build_operation(
    operation: Operation,
    path: str,
    service: Service,
    components: ComponentsRegistry,
    enricher,
    inline_fragments: list[dict],
    seen_operation_ids: set[str],
    error_schema: dict | None,
    config: AgentConfig,
) -> dict[str, Any]:
    enrichment = enricher.enrich_operation(operation, path, service)

    op_obj: dict[str, Any] = {
        "operationId": _operation_id(operation.method, path, seen_operation_ids)
    }
    tags = enrichment.tags or operation.tags_hint
    if tags:
        op_obj["tags"] = list(dict.fromkeys(tags))
    op_obj["summary"] = enrichment.summary
    if enrichment.description:
        op_obj["description"] = enrichment.description
    if operation.deprecated:
        op_obj["deprecated"] = True

    parameters = _build_parameters(operation, path, components, enricher, inline_fragments)
    if parameters:
        op_obj["parameters"] = parameters

    if operation.request_body is not None:
        content: dict[str, Any] = {}
        for media_type in sorted(operation.request_body.content):
            contract = operation.request_body.content[media_type]
            schema = components.rewrite_inline(contract.schema_)
            if not schema:
                # never emit an empty {} body schema: fall back to a free-form object
                schema = dict(_FREEFORM_OBJECT)
            inline_fragments.append(schema)
            media_obj: dict[str, Any] = {"schema": schema}
            if contract.encoding:
                media_obj["encoding"] = {
                    part: _encoding_object(spec) for part, spec in sorted(contract.encoding.items())
                }
            if contract.example is not None:
                media_obj["example"] = contract.example
            content[media_type] = media_obj
        if content:
            body_obj: dict[str, Any] = {"content": content}
            if operation.request_body.required:
                body_obj["required"] = True
            description = enrichment.request_body_description or operation.request_body.description_hint
            if description:
                body_obj["description"] = description
            op_obj["requestBody"] = body_obj

    op_obj["responses"] = _build_responses(operation, components, enrichment, inline_fragments)

    if operation.security:
        requirements = [
            {evidence.scheme_id: sorted(evidence.scopes)}
            for evidence in operation.security
            if evidence.scheme_id in service.security_schemes
            and service.security_schemes[evidence.scheme_id].kind != "custom"
        ]
        if requirements:
            op_obj["security"] = requirements

    if config.openapi.conventional_responses:
        _add_conventional_responses(op_obj, operation, path, error_schema, inline_fragments)

    if config.openapi.annotate_low_confidence and operation.confidence.level == "low":
        # propagate per-item confidence so consumers/CI can flag or hold it
        op_obj["x-openapi-agent"] = {
            "confidence": operation.confidence.level,
            "reason": operation.confidence.reason_code,
        }

    op_obj["responses"] = dict(sorted(op_obj["responses"].items(), key=lambda kv: _status_sort_key(kv[0])))
    return op_obj


def _build_parameters(
    operation: Operation,
    path: str,
    components: ComponentsRegistry,
    enricher,
    inline_fragments: list[dict],
) -> list[dict[str, Any]]:
    parameters: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for parameter in operation.parameters:
        key = (parameter.name, parameter.location)
        if key in seen:
            continue
        seen.add(key)
        schema = components.rewrite_inline(parameter.schema_) if parameter.schema_ else {}
        if not schema:
            # parameters serialize as strings by default — never emit empty {}
            schema = {"type": "string"}
        inline_fragments.append(schema)
        param_obj: dict[str, Any] = {
            "name": parameter.name,
            "in": parameter.location,
            "required": parameter.required,
            "schema": schema,
        }
        description = enricher.parameter_description(parameter, operation, path)
        if description:
            param_obj["description"] = description
        if parameter.style:
            param_obj["style"] = parameter.style
        if parameter.explode is not None:
            param_obj["explode"] = parameter.explode
        if parameter.deprecated:
            param_obj["deprecated"] = True
        parameters.append(param_obj)

    # completeness gate: every templated path parameter must be declared.
    declared_path_params = {p["name"] for p in parameters if p["in"] == "path"}
    for match in re.finditer(r"\{([^}/]+)\}", path):
        name = match.group(1)
        if name not in declared_path_params:
            parameters.append(
                {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            )
    return parameters


def _build_responses(
    operation: Operation,
    components: ComponentsRegistry,
    enrichment,
    inline_fragments: list[dict],
) -> dict[str, Any]:
    """Merge response variants sharing a status code into single response
    objects (multiple distinct shapes for one status+media become ``anyOf``).
    Empty ``{}`` schemas are dropped, leaving a clean no-content response."""
    by_status: dict[str, list[ResponseVariant]] = {}
    for variant in operation.responses:
        by_status.setdefault(variant.status, []).append(variant)

    responses: dict[str, Any] = {}
    for status in sorted(by_status, key=_status_sort_key):
        variants = by_status[status]
        response_obj: dict[str, Any] = {}

        description = (
            enrichment.response_descriptions.get(status)
            or next((v.description_hint for v in variants if v.description_hint), None)
            or status_phrase(status)
        )
        response_obj["description"] = description

        content: dict[str, Any] = {}
        media_schemas: dict[str, list[dict]] = {}
        for variant in variants:
            for media_type, contract in variant.content.items():
                schema = components.rewrite_inline(contract.schema_)
                if not schema:
                    continue  # drop empty {} schema rather than emitting it
                bucket = media_schemas.setdefault(media_type, [])
                if schema not in bucket:
                    bucket.append(schema)
        for media_type in sorted(media_schemas):
            schemas = media_schemas[media_type]
            merged = schemas[0] if len(schemas) == 1 else {"anyOf": schemas}
            inline_fragments.append(merged)
            content[media_type] = {"schema": merged}
        if content:
            response_obj["content"] = content

        headers: dict[str, Any] = {}
        for variant in variants:
            for name, spec in variant.headers.items():
                if name in headers:
                    continue
                schema = components.rewrite_inline(spec.schema_) if spec.schema_ else {"type": "string"}
                inline_fragments.append(schema)
                header_obj: dict[str, Any] = {"schema": schema}
                if spec.required:
                    header_obj["required"] = True
                headers[name] = header_obj
        if headers:
            response_obj["headers"] = dict(sorted(headers.items()))

        responses[status] = response_obj

    if not responses:
        responses["default"] = {"description": status_phrase("default")}
    return responses


def _add_conventional_responses(
    op_obj: dict[str, Any],
    operation: Operation,
    path: str,
    error_schema: dict | None,
    inline_fragments: list[dict],
) -> None:
    """Add conventional error responses grounded in method + auth.

    Only status *keys* are conventional; the referenced body is the service's
    real error schema (or omitted). Existing responses are never overwritten.
    """
    responses = op_obj["responses"]
    conventional: list[tuple[str, str]] = []
    if operation.security:
        conventional.append(("401", "Authentication required"))
        conventional.append(("403", "Insufficient permissions"))
    if "{" in path:
        conventional.append(("404", "Resource not found"))
    conventional.append(("default", "Unexpected error"))

    for status, phrase in conventional:
        if status in responses:
            continue
        response_obj: dict[str, Any] = {"description": phrase}
        if error_schema is not None:
            schema = copy.deepcopy(error_schema)
            inline_fragments.append(schema)
            response_obj["content"] = {"application/json": {"schema": schema}}
        responses[status] = response_obj


def _service_error_schema(endpoints: list[Endpoint], components: ComponentsRegistry) -> dict | None:
    """The most-referenced error schema across >=400 responses, as a rewritten
    ``#/components/schemas/...`` ref (so conventional responses reuse it)."""
    counts: dict[str, int] = {}
    for endpoint in endpoints:
        for operation in endpoint.operations:
            for response in operation.responses:
                if not (response.status.isdigit() and int(response.status) >= 400):
                    continue
                for contract in response.content.values():
                    ref = contract.schema_.get("$ref") if isinstance(contract.schema_, dict) else None
                    if isinstance(ref, str):
                        counts[ref] = counts.get(ref, 0) + 1
    if not counts:
        return None
    best = max(counts, key=lambda k: (counts[k], k))
    return components.rewrite_inline({"$ref": best})


def _status_sort_key(status: str) -> tuple[int, str]:
    try:
        return (int(status), "")
    except ValueError:
        return (1000, status)


def _encoding_object(spec) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    if spec.content_type:
        obj["contentType"] = spec.content_type
    return obj


# ---------------------------------------------------------------------------
# operationId + path helpers
# ---------------------------------------------------------------------------


def _pascal(token: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", token)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


_API_BASE_SEGMENT = re.compile(r"^(?:api|v\d+)$", re.IGNORECASE)


def _operation_id(method: str, path: str, seen: set[str]) -> str:
    """Clean, stable, collision-resistant id: ``<method><Resource…>``.

    Only the API/version base (``api``, ``v1``, …) is dropped; the resource
    segments are always kept, so ids stay unique *across* services (``getBudgets``
    vs ``getCategories``) — never bare ``get``/``post`` that collide in a merged
    spec or generated SDK. Path variables render as ``By<Name>``. No package or
    class names ever leak in."""
    segments = [s for s in path.strip("/").split("/") if s]
    start = 0
    while start < len(segments) and _API_BASE_SEGMENT.match(segments[start]):
        start += 1
    # if stripping the base would leave nothing, keep the full path instead
    if start >= len(segments):
        start = 0
    words: list[str] = []
    for segment in segments[start:]:
        if segment.startswith("{") and segment.endswith("}"):
            words.append("By" + _pascal(segment.strip("{}")))
        else:
            words.append(_pascal(segment))
    base = method.lower() + "".join(words)
    base = re.sub(r"[^A-Za-z0-9_]", "", base) or method.lower()
    candidate = base
    n = 1
    while candidate in seen:
        n += 1
        candidate = f"{base}_{n}"
    seen.add(candidate)
    return candidate


def _common_path_prefix(paths: list[str]) -> str:
    """Longest shared ``/``-delimited, non-templated segment prefix."""
    segment_lists = [
        [s for s in p.strip("/").split("/") if s] for p in paths if p and p != "/"
    ]
    if len(segment_lists) < 1:
        return ""
    common: list[str] = []
    for parts in zip(*segment_lists):
        first = parts[0]
        if first.startswith("{") or any(p != first for p in parts):
            break
        common.append(first)
    return ("/" + "/".join(common)) if common else ""


def _url_path(url: str) -> str:
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/]+(/.*)$", url)
    if match:
        return match.group(1).rstrip("/")
    if url.startswith("/"):
        return url.rstrip("/")
    return ""


def _prefix_matches(server_path: str, common: str) -> bool:
    sp = server_path.strip("/")
    cp = common.strip("/")
    return bool(sp) and (cp == sp or cp.startswith(sp + "/") or sp.startswith(cp + "/") or cp == sp)


def _anchor_patterns(node: Any) -> None:
    """Anchor every ``pattern`` in the document so it matches the whole value."""
    if isinstance(node, dict):
        pattern = node.get("pattern")
        if isinstance(pattern, str) and pattern:
            node["pattern"] = _anchor(pattern)
        for value in node.values():
            _anchor_patterns(value)
    elif isinstance(node, list):
        for item in node:
            _anchor_patterns(item)


def _anchor(pattern: str) -> str:
    anchored = pattern
    if not anchored.startswith("^"):
        anchored = ("^" + anchored) if anchored.endswith("$") else ("^(?:" + anchored + ")")
    if not anchored.endswith("$"):
        anchored = anchored + "$"
    return anchored


# ---------------------------------------------------------------------------
# security schemes
# ---------------------------------------------------------------------------


def _build_security_schemes(service: Service) -> dict[str, Any]:
    schemes: dict[str, Any] = {}
    for scheme_id in sorted(service.security_schemes):
        decl = service.security_schemes[scheme_id]
        mapped = _map_scheme(decl)
        if mapped is not None:
            schemes[scheme_id] = mapped
        else:
            log.warning("security scheme %s (kind=custom) omitted from document", scheme_id)
    return schemes


def _map_scheme(decl: SecuritySchemeDecl) -> dict[str, Any] | None:
    kind = decl.kind
    detail = decl.detail
    if kind == "http_bearer":
        scheme: dict[str, Any] = {"type": "http", "scheme": "bearer"}
        if detail.get("bearerFormat"):
            scheme["bearerFormat"] = detail["bearerFormat"]
        return scheme
    if kind == "http_basic":
        return {"type": "http", "scheme": "basic"}
    if kind in ("apikey_header", "apikey_query", "apikey_cookie"):
        location = {"apikey_header": "header", "apikey_query": "query", "apikey_cookie": "cookie"}[kind]
        name = detail.get("name")
        if not name:
            return None  # unprovable header/query name: omit rather than invent
        return {"type": "apiKey", "in": location, "name": name}
    if kind == "oauth2":
        flows = detail.get("flows")
        if not flows:
            return None
        return {"type": "oauth2", "flows": flows}
    if kind == "openid_connect":
        url = detail.get("openIdConnectUrl")
        if not url:
            return None
        return {"type": "openIdConnect", "openIdConnectUrl": url}
    if kind == "mutual_tls":
        return {"type": "mutualTLS"}
    return None
