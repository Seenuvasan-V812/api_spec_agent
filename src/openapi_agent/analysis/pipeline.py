"""Phase-1 orchestrator: repo facts → adapters → metadata document (atomic JSON).

Flow: pre-scan → language decision → adapter activation → per-adapter
service/route/operation extraction → endpoint assembly → registry
finalization → canonicalize → coverage → schema self-validation → atomic write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.console import Console

import openapi_agent
from openapi_agent.analysis.base import (
    AnalysisContext,
    FrameworkAdapter,
    OperationExtraction,
    UnresolvedSite,
    WarningSink,
    load_adapters,
    select_adapters,
)
from openapi_agent.config.loader import AgentConfig
from openapi_agent.detection.language import decide_language, resolve_ambiguity_interactively
from openapi_agent.detection.repo import RepoFacts, build_repo_facts
from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import (
    CoverageMetrics,
    Endpoint,
    GeneratorInfo,
    MetadataDocument,
    RepoInfo,
    Service,
    UnresolvedContract,
    canonicalize,
    to_canonical_json,
)
from openapi_agent.models.registry import SchemaRegistryBuilder, finalize_document

log = get_logger("analysis.pipeline")


def run_analysis(config: AgentConfig, console: Console | None = None) -> MetadataDocument:
    console = console or Console()
    facts = build_repo_facts(config)
    decision = decide_language(facts, forced=config.analysis.language)
    if decision.ambiguous:
        decision = resolve_ambiguity_interactively(decision)
    console.print(f"[dim]language: {', '.join(decision.languages)} ({decision.rationale})[/dim]")

    warnings = WarningSink()
    registry = SchemaRegistryBuilder()
    base_ctx = AnalysisContext(
        repo_root=config.project_root.resolve(),
        repo_facts=facts,
        config=config,
        warnings=warnings,
        registry=registry,
    )

    for flag, name in (
        (config.analysis.use_pyright, "pyright"),
        (config.analysis.use_mypy, "mypy"),
        (config.analysis.scip_index, "SCIP index"),
    ):
        if flag:
            warnings.emit(
                "W006",
                f"{name} precision booster is configured but not implemented in this "
                "version; analysis proceeds with the standard toolchain",
            )

    adapters = load_adapters(only=config.analysis.frameworks)
    adapters = [a for a in adapters if a.language in decision.languages]
    activated = select_adapters(adapters, facts)
    if not activated:
        warnings.emit("W001", "no framework adapter activated; document will be empty")

    services: list[Service] = []
    unresolved_sites: list[UnresolvedSite] = []
    contexts: dict[str, AnalysisContext] = {}

    with _python_sys_path(config, decision.languages):
        for adapter, _detection in activated:
            ctx = _context_for(adapter, base_ctx, contexts)
            try:
                adapter_services = adapter.discover_services(ctx)
            except Exception as exc:  # noqa: BLE001 - one adapter must not sink the run
                log.exception("adapter %s failed during service discovery", adapter.name)
                warnings.emit("W002", f"adapter {adapter.name} failed: {exc}")
                continue
            if config.analysis.services:
                adapter_services = [
                    s for s in adapter_services if s.id in config.analysis.services
                ]
            for service in adapter_services:
                _extract_service(adapter, ctx, service, unresolved_sites, console)
                services.append(service)

    document = MetadataDocument(
        metadata_version=openapi_agent.METADATA_VERSION,
        generator=GeneratorInfo(tool_version=openapi_agent.__version__),
        repo=RepoInfo(
            vcs_commit=_git_commit(config.project_root),
            languages=sorted(decision.languages),
            build_systems=sorted({m.kind for m in facts.manifests if m.kind in ("pyproject", "requirements", "pom", "gradle")}),
            analyzed_roots=sorted({s.root_path or "." for s in services}) or ["."],
        ),
        services=services,
        warnings=[],
        unresolved=[],
    )
    finalize_document(document, registry)
    canonicalize(document)
    document.warnings = sorted(warnings.warnings, key=lambda w: w.sort_key())
    document.unresolved = _resolve_pointers(document, unresolved_sites)
    document.coverage = _compute_coverage(document, facts, warnings)
    canonicalize(document)

    _self_validate(document, warnings)
    _atomic_write(config.output.metadata_path, to_canonical_json(document))
    log.info("metadata written to %s", config.output.metadata_path)
    return document


def _extract_service(
    adapter: FrameworkAdapter,
    ctx: AnalysisContext,
    service: Service,
    unresolved_sites: list[UnresolvedSite],
    console: Console,
) -> None:
    try:
        routes = adapter.discover_routes(ctx, service)
    except Exception as exc:  # noqa: BLE001
        log.exception("route discovery failed for %s", service.id)
        ctx.warnings.emit("W003", f"route discovery failed: {exc}", service_id=service.id)
        return
    console.print(f"[dim]service {service.id}: {len(routes)} route declarations[/dim]")

    endpoints: dict[str, Endpoint] = {}
    seen_operation_ids: set[str] = set()
    for route in routes:
        try:
            extractions = adapter.extract_operation(ctx, service, route)
        except Exception as exc:  # noqa: BLE001 - degrade, never sink the document
            log.exception("extraction failed for %s %s", route.raw_path, route.handler_symbol)
            ctx.warnings.emit(
                "W004",
                f"operation extraction failed for {route.raw_path}: {exc}",
                file=route.file,
                start_line=route.start_line,
                service_id=service.id,
            )
            continue
        for extraction in extractions:
            operation = extraction.operation
            base_id = operation.operation_id
            n = 1
            while operation.operation_id in seen_operation_ids:
                n += 1
                operation.operation_id = f"{base_id}_{n}"
            seen_operation_ids.add(operation.operation_id)
            endpoint = endpoints.get(extraction.endpoint_path)
            if endpoint is None:
                endpoint = Endpoint(
                    path=extraction.endpoint_path,
                    raw_path=extraction.raw_path,
                    operations=[],
                )
                endpoints[extraction.endpoint_path] = endpoint
            existing = {o.method for o in endpoint.operations}
            if operation.method in existing:
                ctx.warnings.emit(
                    "W005",
                    f"duplicate {operation.method.upper()} {extraction.endpoint_path}; keeping first",
                    file=route.file,
                    start_line=route.start_line,
                    service_id=service.id,
                )
                continue
            endpoint.operations.append(operation)
            unresolved_sites.extend(extraction.unresolved)
    service.endpoints = list(endpoints.values())
    # dedupe dependency edges collected during extraction
    unique_edges = {(e.from_file, e.to_file, e.kind): e for e in service.dependencies}
    service.dependencies = list(unique_edges.values())


def _context_for(
    adapter: FrameworkAdapter, base: AnalysisContext, cache: dict[str, AnalysisContext]
) -> AnalysisContext:
    if adapter.language == "python":
        if "python" not in cache:
            import astroid

            astroid.MANAGER.clear_cache()
            from openapi_agent.analysis.python.context import build_python_context

            cache["python"] = build_python_context(base)
        return cache["python"]
    if adapter.language == "java":
        if "java" not in cache:
            from openapi_agent.analysis.java.context import build_java_context

            cache["java"] = build_java_context(base)
        return cache["java"]
    return base


class _python_sys_path:
    """Temporarily expose the target repo to astroid's import finder.

    astroid only *parses* modules found on sys.path — target code is never
    executed. Removed on exit so repeated runs (tests) stay isolated.
    """

    def __init__(self, config: AgentConfig, languages: list[str]) -> None:
        self.paths: list[str] = []
        if "python" in languages:
            root = str(config.project_root.resolve())
            self.paths = [root]
            for extra in ("src",):
                candidate = Path(root) / extra
                if candidate.is_dir():
                    self.paths.append(str(candidate))

    def __enter__(self):
        for path in self.paths:
            if path not in sys.path:
                sys.path.insert(0, path)
        return self

    def __exit__(self, *exc):
        for path in self.paths:
            if path in sys.path:
                sys.path.remove(path)
        return False


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        commit = result.stdout.strip()
        return commit if result.returncode == 0 and len(commit) == 40 else None
    except Exception:  # noqa: BLE001
        return None


def _resolve_pointers(
    document: MetadataDocument, sites: list[UnresolvedSite]
) -> list[UnresolvedContract]:
    """Convert logical unresolved sites to JSON Pointers into the final doc."""
    service_index = {s.id: i for i, s in enumerate(document.services)}
    contracts: list[UnresolvedContract] = []
    for site in sites:
        si = service_index.get(site.service_id)
        if si is None:
            continue
        service = document.services[si]
        ei = next((i for i, e in enumerate(service.endpoints) if e.path == site.path), None)
        if ei is None:
            continue
        endpoint = service.endpoints[ei]
        oi = next(
            (i for i, o in enumerate(endpoint.operations) if o.method == site.method), None
        )
        if oi is None:
            continue
        operation = endpoint.operations[oi]
        pointer = f"/services/{si}/endpoints/{ei}/operations/{oi}/{_index_site(operation, site.site)}"
        contracts.append(
            UnresolvedContract(
                pointer=pointer,
                kind=site.kind,
                reason_code=site.reason_code,
                evidence=site.evidence,
            )
        )
    return contracts


def _index_site(operation, site: str) -> str:
    """Adapters name response/parameter sites by status/name; JSON Pointers
    need list indexes. Translate the first two segments when applicable."""
    segments = site.split("/")
    if len(segments) >= 2 and segments[0] == "responses":
        index = next(
            (i for i, r in enumerate(operation.responses) if r.status == segments[1]), None
        )
        if index is not None:
            segments[1] = str(index)
    elif len(segments) >= 2 and segments[0] == "parameters":
        index = next(
            (i for i, p in enumerate(operation.parameters) if p.name == segments[1]), None
        )
        if index is not None:
            segments[1] = str(index)
    return "/".join(segments)


def _compute_coverage(
    document: MetadataDocument, facts: RepoFacts, warnings: WarningSink
) -> CoverageMetrics:
    metrics = CoverageMetrics(
        files_scanned=len(facts.python_files) + len(facts.java_files),
        files_failed_parse=len(warnings.files_failed_parse),
        unresolved_count=len(document.unresolved),
    )
    confidence_counts = {"high": 0, "medium": 0, "low": 0}

    def count(confidence) -> None:
        confidence_counts[confidence.level] += 1

    for service in document.services:
        metrics.endpoints_total += len(service.endpoints)
        for endpoint in service.endpoints:
            for operation in endpoint.operations:
                metrics.operations_total += 1
                count(operation.confidence)
                if operation.method in ("post", "put", "patch"):
                    metrics.operations_needing_body += 1
                if operation.request_body is not None:
                    count(operation.request_body.confidence)
                    if any(
                        contract.schema_ for contract in operation.request_body.content.values()
                    ):
                        metrics.operations_with_request_schema += 1
                for parameter in operation.parameters:
                    metrics.parameters_total += 1
                    count(parameter.confidence)
                    if parameter.schema_:
                        metrics.parameters_typed += 1
                for response in operation.responses:
                    metrics.responses_total += 1
                    count(response.confidence)
                    if any(contract.schema_ for contract in response.content.values()) or not response.content:
                        metrics.responses_with_schema += 1
                if operation.security:
                    metrics.operations_with_security += 1
    for entry in document.schema_registry.schemas.values():
        count(entry.confidence)
    metrics.confidence_counts = confidence_counts
    return metrics


def _self_validate(document: MetadataDocument, warnings: WarningSink) -> None:
    """Validate the emitted document against the published metadata schema."""
    try:
        import jsonschema

        from openapi_agent.models.metadata import export_metadata_schema

        payload = json.loads(to_canonical_json(document))
        jsonschema.validate(
            payload,
            export_metadata_schema(),
            cls=jsonschema.validators.Draft202012Validator,
        )
    except jsonschema.ValidationError as exc:  # pragma: no cover - guards regressions
        warnings.emit("W009", f"metadata failed self-validation: {exc.message}")
        log.error("metadata self-validation failed: %s", exc.message)
    except Exception as exc:  # noqa: BLE001
        log.debug("self-validation skipped: %s", exc)


def _atomic_write(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
