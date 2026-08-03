"""Layered validation & programmatic gates.

Layers:
1. syntax        - ruamel/json round-trip (also enforced by the writer)
2. structural    - openapi-spec-validator (3.1) + JSON Schema 2020-12
                   metaschema validation of every schema object
3. references    - prance resolution when available, plus an internal pointer
                   resolver that is always run (the authoritative gate)
4. lint          - optional Redocly CLI / Spectral via npx (config-gated,
                   skipped with a warning when Node tooling is absent)
5. smoke         - optional schemathesis schema-load check (config-gated)

Gates (require the Phase-1 metadata): unique operationIds, declared path
params, response-key validity, 100% endpoint coverage, and zero invention
(no path/method/status/parameter/security claim absent from metadata).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openapi_agent.config.loader import AgentConfig
from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import MetadataDocument, Service

log = get_logger("openapi.validators")

_STATUS_KEY_RE = re.compile(r"^([1-5]\d{2}|[1-5]XX|default)$")
_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


class ValidationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning", "info"]
    layer: str
    text: str


class ValidationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    messages: list[ValidationMessage] = Field(default_factory=list)
    gates: dict[str, bool] = Field(default_factory=dict)

    def add(self, severity: str, layer: str, text: str) -> None:
        self.messages.append(ValidationMessage(severity=severity, layer=layer, text=text))  # type: ignore[arg-type]
        if severity == "error":
            self.ok = False

    def gate(self, name: str, passed: bool, detail: str = "") -> None:
        self.gates[name] = passed
        if not passed:
            self.add("error", "gates", f"gate {name} failed{': ' + detail if detail else ''}")


# ---------------------------------------------------------------------------
# structural layers
# ---------------------------------------------------------------------------


def validate_structure(document: dict[str, Any], outcome: ValidationOutcome) -> None:
    try:
        from openapi_spec_validator import validate as osv_validate

        osv_validate(document)
        outcome.add("info", "structural", "openapi-spec-validator: OK")
    except Exception as exc:  # noqa: BLE001
        message = getattr(exc, "message", None) or str(exc)
        outcome.add("error", "structural", f"openapi-spec-validator: {message[:400]}")

    # JSON Schema 2020-12 metaschema validation of every schema object
    try:
        import jsonschema

        validator = jsonschema.validators.Draft202012Validator(
            jsonschema.validators.Draft202012Validator.META_SCHEMA
        )
        bad = 0
        for location, schema in _iter_schema_objects(document):
            for error in validator.iter_errors(_strip_oas_keywords(schema)):
                bad += 1
                if bad <= 5:
                    outcome.add(
                        "error", "structural", f"schema at {location} violates 2020-12: {error.message[:200]}"
                    )
        if bad == 0:
            outcome.add("info", "structural", "JSON Schema 2020-12 metaschema: OK")
    except Exception as exc:  # noqa: BLE001
        outcome.add("warning", "structural", f"metaschema validation unavailable: {exc}")


def _strip_oas_keywords(schema: dict) -> dict:
    # `discriminator` is an OAS keyword allowed inside schema objects
    return {k: v for k, v in schema.items() if k != "discriminator"}


def _iter_schema_objects(document: dict[str, Any]):
    for name, schema in (document.get("components", {}).get("schemas", {}) or {}).items():
        yield f"components.schemas.{name}", schema
    for path, item in (document.get("paths", {}) or {}).items():
        for method, operation in item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []) or []:
                if isinstance(parameter.get("schema"), dict):
                    yield f"{method.upper()} {path} param {parameter.get('name')}", parameter["schema"]
            body = operation.get("requestBody", {})
            for media, media_obj in (body.get("content", {}) or {}).items():
                if isinstance(media_obj.get("schema"), dict):
                    yield f"{method.upper()} {path} body {media}", media_obj["schema"]
            for status, response in (operation.get("responses", {}) or {}).items():
                for media, media_obj in (response.get("content", {}) or {}).items():
                    if isinstance(media_obj.get("schema"), dict):
                        yield f"{method.upper()} {path} {status} {media}", media_obj["schema"]


# ---------------------------------------------------------------------------
# reference resolution
# ---------------------------------------------------------------------------


def validate_refs(document: dict[str, Any], outcome: ValidationOutcome) -> None:
    unresolved = sorted(_find_unresolved_refs(document))
    if unresolved:
        for ref in unresolved[:10]:
            outcome.add("error", "references", f"unresolvable $ref: {ref}")
    else:
        outcome.add("info", "references", "all $refs resolve")

    try:
        import prance

        prance.ResolvingParser(
            spec_string=json.dumps(document), backend="openapi-spec-validator", lazy=False
        )
        outcome.add("info", "references", "prance resolution: OK")
    except ImportError:
        outcome.add("warning", "references", "prance not installed; internal resolver only")
    except Exception as exc:  # noqa: BLE001
        # prance struggles with some valid 3.1 constructs; internal resolver is
        # the authoritative gate, so this is a warning unless it too failed.
        severity = "warning" if not unresolved else "error"
        outcome.add(severity, "references", f"prance: {str(exc)[:300]}")


def _find_unresolved_refs(document: dict[str, Any]) -> set[str]:
    unresolved: set[str] = set()

    def resolve(pointer: str) -> bool:
        if not pointer.startswith("#/"):
            return False  # external refs are never emitted
        node: Any = document
        for raw in pointer[2:].split("/"):
            key = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and key in node:
                node = node[key]
            elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
                node = node[int(key)]
            else:
                return False
        return True

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not resolve(ref):
                unresolved.add(ref)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document)
    return unresolved


# ---------------------------------------------------------------------------
# optional external linters / smoke check
# ---------------------------------------------------------------------------


def _run_node_linter(args: list[str], layer: str, outcome: ValidationOutcome) -> None:
    npx = shutil.which("npx")
    if npx is None:
        outcome.add("warning", layer, "Node tooling not found on PATH; lint skipped")
        return
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [npx, "--yes", *args],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode == 0:
            outcome.add("info", layer, "lint passed")
        else:
            tail = (result.stdout + result.stderr).strip()[-800:]
            outcome.add("warning", layer, f"lint reported issues:\n{tail}")
    except Exception as exc:  # noqa: BLE001
        outcome.add("warning", layer, f"lint skipped ({exc})")


def run_optional_layers(spec_path: Path, config: AgentConfig, outcome: ValidationOutcome) -> None:
    if config.validation.redocly_lint:
        _run_node_linter(["@redocly/cli", "lint", str(spec_path)], "redocly", outcome)
    if config.validation.spectral_lint:
        _run_node_linter(
            ["@stoplight/spectral-cli", "lint", "--ruleset", "spectral:oas", str(spec_path)],
            "spectral",
            outcome,
        )
    if config.validation.schemathesis_smoke:
        try:
            import schemathesis

            loader = getattr(getattr(schemathesis, "openapi", schemathesis), "from_path", None)
            if loader is None:
                loader = getattr(schemathesis, "from_path", None)
            if loader is None:
                raise RuntimeError("no from_path loader in this schemathesis version")
            loader(str(spec_path))
            outcome.add("info", "schemathesis", "schema loads in schemathesis")
        except ImportError:
            outcome.add("warning", "schemathesis", "schemathesis not installed; smoke check skipped")
        except Exception as exc:  # noqa: BLE001
            outcome.add("error", "schemathesis", f"schema failed to load: {str(exc)[:300]}")


# ---------------------------------------------------------------------------
# programmatic gates vs Phase-1 metadata
# ---------------------------------------------------------------------------


def run_gates(
    document: dict[str, Any],
    metadata: MetadataDocument,
    service: Service,
    outcome: ValidationOutcome,
    strict: bool,
    config: AgentConfig | None = None,
) -> None:
    from openapi_agent.openapi.builder import is_internal_path

    internal_prefixes = config.openapi.internal_path_prefixes if config else []
    # only tolerate conventional error/default keys when the builder was
    # explicitly configured to add them; with no config, stay strict.
    conventional_statuses = (
        {"401", "403", "404", "500", "503", "default"}
        if (config is not None and config.openapi.conventional_responses)
        else set()
    )
    paths = document.get("paths", {}) or {}

    # unique operation ids
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in paths.values():
        for method, operation in item.items():
            if method in _HTTP_METHODS and isinstance(operation, dict):
                operation_id = operation.get("operationId", "")
                if operation_id in seen:
                    duplicates.append(operation_id)
                seen.add(operation_id)
    outcome.gate("unique_operation_ids", not duplicates, ", ".join(duplicates[:5]))

    # every templated path parameter declared & required
    missing_params: list[str] = []
    for path, item in paths.items():
        templated = set(re.findall(r"\{([^}/]+)\}", path))
        for method, operation in item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            declared = {
                p["name"]
                for p in operation.get("parameters", []) or []
                if p.get("in") == "path" and p.get("required") is True
            }
            for name in templated - declared:
                missing_params.append(f"{method.upper()} {path} {{{name}}}")
    outcome.gate("path_params_declared", not missing_params, ", ".join(missing_params[:5]))

    # response keys valid + one response object per key (dict by construction)
    bad_statuses = [
        f"{method.upper()} {path} -> {status}"
        for path, item in paths.items()
        for method, operation in item.items()
        if method in _HTTP_METHODS and isinstance(operation, dict)
        for status in (operation.get("responses", {}) or {})
        if not _STATUS_KEY_RE.match(str(status))
    ]
    outcome.gate("valid_status_keys", not bad_statuses, ", ".join(bad_statuses[:5]))

    # coverage + zero invention against metadata (internal paths are excluded
    # from the public document by design, so they are not "omitted")
    metadata_ops: dict[tuple[str, str], Any] = {}
    for endpoint in service.endpoints:
        if is_internal_path(endpoint.path, internal_prefixes):
            continue
        for operation in endpoint.operations:
            metadata_ops[(endpoint.path, operation.method)] = operation

    document_ops = {
        (path, method)
        for path, item in paths.items()
        for method in item
        if method in _HTTP_METHODS
    }

    omitted = sorted(f"{m.upper()} {p}" for (p, m) in set(metadata_ops) - document_ops)
    invented_ops = sorted(f"{m.upper()} {p}" for (p, m) in document_ops - set(metadata_ops))
    outcome.gate("endpoint_coverage", not omitted, ", ".join(omitted[:5]))
    outcome.gate("no_invented_operations", not invented_ops, ", ".join(invented_ops[:5]))

    invented_details: list[str] = []
    altered_high_confidence: list[str] = []
    for (path, method) in document_ops & set(metadata_ops):
        operation_meta = metadata_ops[(path, method)]
        doc_operation = paths[path][method]
        label = f"{method.upper()} {path}"

        allowed_statuses = {v.status for v in operation_meta.responses} or {"default"}
        if not operation_meta.responses:
            allowed_statuses = {"default"}
        allowed_statuses |= conventional_statuses  # conventional error/default keys
        for status in doc_operation.get("responses", {}) or {}:
            if str(status) not in allowed_statuses:
                invented_details.append(f"{label}: status {status}")

        allowed_params = {(p.name, p.location) for p in operation_meta.parameters}
        templated = set(re.findall(r"\{([^}/]+)\}", path))
        for parameter in doc_operation.get("parameters", []) or []:
            key = (parameter.get("name"), parameter.get("in"))
            if key not in allowed_params and not (
                parameter.get("in") == "path" and parameter.get("name") in templated
            ):
                invented_details.append(f"{label}: parameter {key[0]} in {key[1]}")

        allowed_schemes = {s.scheme_id for s in operation_meta.security}
        for requirement in doc_operation.get("security", []) or []:
            for scheme in requirement:
                if scheme not in allowed_schemes:
                    invented_details.append(f"{label}: security scheme {scheme}")

        if ("requestBody" in doc_operation) and operation_meta.request_body is None:
            invented_details.append(f"{label}: requestBody")

        if strict and operation_meta.confidence.level == "high":
            metadata_statuses = {v.status for v in operation_meta.responses}
            doc_statuses = {str(s) for s in doc_operation.get("responses", {}) or {}}
            if not metadata_statuses <= doc_statuses:
                altered_high_confidence.append(label)

    outcome.gate("no_invented_details", not invented_details, "; ".join(invented_details[:5]))
    if strict:
        outcome.gate(
            "strict_high_confidence_preserved",
            not altered_high_confidence,
            ", ".join(altered_high_confidence[:5]),
        )

    low_confidence = metadata.coverage.confidence_counts.get("low", 0)
    if low_confidence:
        outcome.add(
            "warning",
            "gates",
            f"{low_confidence} low-confidence contract(s) use free-form schema fallbacks",
        )


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def validate_document_dict(
    document: dict[str, Any],
    config: AgentConfig,
    metadata: Optional[MetadataDocument] = None,
    service: Optional[Service] = None,
) -> ValidationOutcome:
    outcome = ValidationOutcome()
    validate_structure(document, outcome)
    validate_refs(document, outcome)
    if metadata is not None and service is not None:
        run_gates(document, metadata, service, outcome, strict=config.validation.strict, config=config)
    return outcome


def validate_document_file(path: Path, config: AgentConfig) -> ValidationOutcome:
    outcome = ValidationOutcome()
    path = Path(path)
    if not path.is_file():
        outcome.add("error", "syntax", f"document not found: {path}")
        return outcome
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            document = json.loads(text)
        else:
            from ruamel.yaml import YAML

            document = YAML(typ="safe").load(text)
        outcome.add("info", "syntax", "document parses")
    except Exception as exc:  # noqa: BLE001
        outcome.add("error", "syntax", f"parse failure: {exc}")
        return outcome
    if not isinstance(document, dict):
        outcome.add("error", "syntax", "top level is not a mapping")
        return outcome
    validate_structure(document, outcome)
    validate_refs(document, outcome)
    run_optional_layers(path, config, outcome)
    return outcome
