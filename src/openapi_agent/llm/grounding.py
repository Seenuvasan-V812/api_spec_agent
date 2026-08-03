"""Grounding validation: reject LLM output that references facts absent from
the operation metadata (paths, status codes, parameters, fields, auth claims)."""

from __future__ import annotations

import json
import re

from openapi_agent.models.metadata import Operation, Service

_STATUS_RE = re.compile(r"\b([1-5]\d{2})\b")
_PATH_RE = re.compile(r"(?<![\w.])(/[A-Za-z0-9_{}\-./]{2,})")
_AUTH_CLAIM_RE = re.compile(
    r"\b(requires? (?:an? )?(?:api key|bearer|oauth|authentication|authorization)|must be authenticated)\b",
    re.IGNORECASE,
)


def collect_vocabulary(operation: Operation, path: str, service: Service) -> set[str]:
    """Lower-cased identifier vocabulary the model is allowed to mention."""
    vocab: set[str] = set()
    for segment in path.split("/"):
        segment = segment.strip("{}")
        if segment:
            vocab.add(segment.lower())
    for parameter in operation.parameters:
        vocab.add(parameter.name.lower())
    for response in operation.responses:
        vocab.add(response.status)
    if operation.request_body:
        for contract in operation.request_body.content.values():
            _schema_names(contract.schema_, vocab)
    for response in operation.responses:
        for contract in response.content.values():
            _schema_names(contract.schema_, vocab)
    for tag in operation.tags_hint:
        vocab.add(tag.lower())
    return vocab


def _schema_names(schema: dict, vocab: set[str]) -> None:
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                for name in value:
                    vocab.add(str(name).lower())
            if key == "$ref" and isinstance(value, str):
                vocab.add(value.rsplit("/", 1)[-1].split("--")[0].rsplit(".", 1)[-1].lower())
            _schema_names(value, vocab)  # type: ignore[arg-type]
    elif isinstance(schema, list):
        for item in schema:
            _schema_names(item, vocab)


def is_grounded(enrichment, operation: Operation, path: str, service: Service) -> tuple[bool, str]:
    """(ok, reason). Applies structural and textual grounding rules."""
    statuses = {v.status for v in operation.responses}

    for status in enrichment.response_descriptions:
        if status not in statuses:
            return False, f"response description for undeclared status {status}"

    parameter_names = {p.name for p in operation.parameters}
    for name in enrichment.parameter_descriptions:
        if name not in parameter_names:
            return False, f"description for undeclared parameter {name!r}"

    texts = [enrichment.summary or "", enrichment.description or ""]
    texts += list(enrichment.response_descriptions.values())
    texts += list(enrichment.parameter_descriptions.values())
    if enrichment.request_body_description:
        texts.append(enrichment.request_body_description)
    blob = " ".join(texts)

    for match in _STATUS_RE.finditer(blob):
        if match.group(1) not in statuses:
            return False, f"mentions undeclared status code {match.group(1)}"

    for match in _PATH_RE.finditer(blob):
        mentioned = match.group(1).rstrip(".,;:)")
        if mentioned.rstrip("/") and not path.startswith(mentioned.rstrip("/")) and mentioned.rstrip("/") not in path:
            return False, f"mentions foreign path {mentioned!r}"

    if not operation.security and _AUTH_CLAIM_RE.search(blob):
        return False, "claims authentication requirement without security evidence"

    if enrichment.tags:
        allowed = collect_vocabulary(operation, path, service)
        for tag in enrichment.tags:
            tag_l = tag.lower().strip()
            if tag_l not in allowed and tag_l.rstrip("s") not in allowed and f"{tag_l}s" not in allowed:
                return False, f"invented tag {tag!r}"
    return True, ""


def compact_operation_payload(operation: Operation, path: str, service: Service) -> str:
    """Compact, source-free metadata sent to the model (never source code)."""
    payload = {
        "method": operation.method.upper(),
        "path": path,
        "handler_name": operation.handler.rsplit(".", 1)[-1],
        "summary_hint": operation.summary_hint,
        "description_hint": operation.description_hint,
        "tags_hint": operation.tags_hint,
        "parameters": [
            {
                "name": p.name,
                "in": p.location,
                "required": p.required,
                "type": p.schema_.get("type") or ("ref" if "$ref" in p.schema_ else None),
                "hint": p.description_hint,
            }
            for p in operation.parameters
        ],
        "request_body": sorted(operation.request_body.content) if operation.request_body else None,
        "request_fields": _top_fields(operation.request_body.content) if operation.request_body else [],
        "responses": [
            {
                "status": v.status,
                "media": sorted(v.content),
                "fields": _top_fields(v.content),
                "hint": v.description_hint,
                "condition": v.condition.exception_type if v.condition else None,
            }
            for v in operation.responses
        ],
        "security": [
            {"scheme": s.scheme_id, "scopes": s.scopes} for s in operation.security
        ],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _top_fields(content: dict) -> list[str]:
    fields: list[str] = []
    for contract in content.values():
        schema = contract.schema_
        if isinstance(schema, dict):
            properties = schema.get("properties")
            if isinstance(properties, dict):
                fields.extend(str(k) for k in properties)
            ref = schema.get("$ref")
            if isinstance(ref, str):
                fields.append(ref.rsplit("/", 1)[-1].split("--")[0].rsplit(".", 1)[-1])
    return sorted(set(fields))[:20]
