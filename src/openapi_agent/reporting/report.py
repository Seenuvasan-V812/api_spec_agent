"""Readiness / coverage report: machine-readable JSON + Rich rendering."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.table import Table

from openapi_agent.config.loader import AgentConfig
from openapi_agent.logging_utils import get_logger
from openapi_agent.openapi.generator import GenerationResult, load_metadata

log = get_logger("reporting")


class ServiceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: str
    spec_path: str
    endpoints: int
    operations: int
    request_completeness: float  # bodies with schemas / operations needing bodies
    response_completeness: float
    parameter_completeness: float
    confidence_counts: dict[str, int] = Field(default_factory=dict)
    unresolved_count: int
    operations_with_security: int = 0
    validation_errors: int
    validation_warnings: int
    gates: dict[str, bool] = Field(default_factory=dict)
    llm_failures: int
    readiness_score: float
    production_ready: bool = True
    blocking_issues: list[str] = Field(default_factory=list)


class ReadinessReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_by: str = "openapi-agent"
    metadata_path: str
    strict_mode: bool
    strict_ok: bool
    llm_provider: str
    extractors_complete: bool = True  # False when a required extractor fell back
    production_ready: bool = True
    services: list[ServiceReport] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    overall_readiness: float = 0.0


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 1.0


def _score(service: ServiceReport) -> float:
    total_confidence = sum(service.confidence_counts.values()) or 1
    confidence_score = (
        service.confidence_counts.get("high", 0) + 0.5 * service.confidence_counts.get("medium", 0)
    ) / total_confidence
    gates_score = (
        sum(1 for passed in service.gates.values() if passed) / len(service.gates)
        if service.gates
        else 1.0
    )
    validation_score = 1.0 if service.validation_errors == 0 else 0.0
    score = (
        0.30 * gates_score
        + 0.20 * validation_score
        + 0.20 * confidence_score
        + 0.15 * service.response_completeness
        + 0.10 * service.request_completeness
        + 0.05 * service.parameter_completeness
    )
    # LLM enrichment failures degrade description prose (not the contract) —
    # apply a modest, capped score penalty so the run is not reported as clean.
    if service.llm_failures:
        score *= max(0.85, 1.0 - 0.02 * service.llm_failures)
    return round(100 * score, 1)


def _service_coverage(service, config: AgentConfig) -> tuple[dict, dict, int]:
    """Per-service coverage counts, confidence counts, and unresolved count —
    scoped to the service's PUBLIC endpoints (internal paths excluded)."""
    from openapi_agent.openapi.builder import public_endpoints

    conf = {"high": 0, "medium": 0, "low": 0}

    def count(c) -> None:
        if c.level in conf:
            conf[c.level] += 1

    m = {
        "ops": 0, "bodies_present": 0, "with_req": 0, "resp": 0, "resp_schema": 0,
        "params": 0, "params_typed": 0, "secured": 0, "bodyless_writes": 0,
    }
    for endpoint in public_endpoints(service, config):
        for op in endpoint.operations:
            m["ops"] += 1
            count(op.confidence)
            if op.request_body is not None:
                # request completeness measures the quality of bodies that
                # actually exist; body-less writes (action POSTs) are not gaps.
                m["bodies_present"] += 1
                count(op.request_body.confidence)
                if any(c.schema_ for c in op.request_body.content.values()):
                    m["with_req"] += 1
            elif op.method in ("post", "put", "patch"):
                m["bodyless_writes"] += 1
            for p in op.parameters:
                m["params"] += 1
                count(p.confidence)
                if p.schema_:
                    m["params_typed"] += 1
            for r in op.responses:
                m["resp"] += 1
                count(r.confidence)
                if any(c.schema_ for c in r.content.values()) or not r.content:
                    m["resp_schema"] += 1
            if op.security:
                m["secured"] += 1
    return m, conf, 0


def _production_gates(
    config: AgentConfig, report: "ServiceReport", service, extractors_complete: bool
) -> list[str]:
    q = config.quality
    issues: list[str] = []
    if report.unresolved_count > q.max_unresolved:
        issues.append(f"{report.unresolved_count} unresolved contract(s) (max {q.max_unresolved})")
    if report.response_completeness < q.min_response_completeness:
        issues.append(f"response completeness {report.response_completeness:.0%} < {q.min_response_completeness:.0%}")
    if report.request_completeness < q.min_request_completeness:
        issues.append(f"request completeness {report.request_completeness:.0%} < {q.min_request_completeness:.0%}")
    if report.parameter_completeness < q.min_parameter_completeness:
        issues.append(f"parameter completeness {report.parameter_completeness:.0%} < {q.min_parameter_completeness:.0%}")
    if report.validation_errors:
        issues.append(f"{report.validation_errors} validation error(s)")
    # LLM-enrichment failures affect only description prose (not the contract),
    # so they block production ONLY when descriptions are explicitly required.
    if q.require_descriptions and report.llm_failures > q.max_llm_failures:
        issues.append(
            f"{report.llm_failures} LLM enrichment failure(s) — summaries/descriptions "
            "fell back to deterministic templates (check the provider key/model)"
        )
    if report.gates and not all(report.gates.values()):
        failed = [k for k, v in report.gates.items() if not v]
        issues.append(f"gate(s) failed: {', '.join(failed)}")
    # auth defined for the service but not applied to any operation
    if service is not None and service.security_schemes and report.operations_with_security == 0:
        issues.append("authentication scheme defined but no operation is secured")
    if q.require_extractors and not extractors_complete:
        issues.append("a required extractor (JVM sidecar) was unavailable")
    return issues


def build_report(config: AgentConfig, results: list[GenerationResult]) -> ReadinessReport:
    metadata = load_metadata(config.output.metadata_path)
    extractors_complete = not any(w.code == "W401" for w in metadata.warnings)
    report = ReadinessReport(
        metadata_path=str(config.output.metadata_path),
        strict_mode=config.validation.strict,
        strict_ok=True,
        llm_provider=config.llm.provider,
        extractors_complete=extractors_complete,
        warnings=[f"{w.code}: {w.message}" for w in metadata.warnings][:100],
    )
    for result in results:
        service_meta = next((s for s in metadata.services if s.id == result.service_id), None)
        errors = sum(1 for m in result.validation.messages if m.severity == "error")
        warnings_count = sum(1 for m in result.validation.messages if m.severity == "warning")
        if service_meta is not None:
            m, conf, unresolved = _service_coverage(service_meta, config)
        else:  # pragma: no cover - result always has metadata
            m = {"ops": 0, "bodies_present": 0, "with_req": 0, "resp": 0, "resp_schema": 0,
                 "params": 0, "params_typed": 0, "secured": 0, "bodyless_writes": 0}
            conf, unresolved = {}, 0
        service_report = ServiceReport(
            service_id=result.service_id,
            spec_path=result.output_path,
            endpoints=result.endpoints,
            operations=result.operations,
            request_completeness=_ratio(m["with_req"], m["bodies_present"]),
            response_completeness=_ratio(m["resp_schema"], m["resp"]),
            parameter_completeness=_ratio(m["params_typed"], m["params"]),
            confidence_counts=conf,
            unresolved_count=unresolved,
            operations_with_security=m["secured"],
            validation_errors=errors,
            validation_warnings=warnings_count,
            gates=result.validation.gates,
            llm_failures=result.llm_failures,
            readiness_score=0.0,
        )
        service_report.readiness_score = _score(service_report)
        service_report.blocking_issues = _production_gates(
            config, service_report, service_meta, extractors_complete
        )
        service_report.production_ready = not service_report.blocking_issues
        report.services.append(service_report)
        if not result.validation.ok:
            report.strict_ok = False
        if config.validation.strict and not all(result.validation.gates.values()):
            report.strict_ok = False
    if report.services:
        report.overall_readiness = round(
            sum(s.readiness_score for s in report.services) / len(report.services), 1
        )
    report.production_ready = extractors_complete or not config.quality.require_extractors
    report.production_ready = report.production_ready and all(s.production_ready for s in report.services)
    return report


def write_report(config: AgentConfig, results: list[GenerationResult]) -> ReadinessReport:
    report = build_report(config, results)
    path = Path(config.output.report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return report


def load_report(path: Path) -> ReadinessReport:
    return ReadinessReport.model_validate_json(Path(path).read_text(encoding="utf-8"))


def render_report_table(report: ReadinessReport, console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title=f"Readiness report — provider: {report.llm_provider}")
    table.add_column("service")
    table.add_column("endpoints", justify="right")
    table.add_column("operations", justify="right")
    table.add_column("req%", justify="right")
    table.add_column("resp%", justify="right")
    table.add_column("high/med/low", justify="center")
    table.add_column("unresolved", justify="right")
    table.add_column("errors", justify="right")
    table.add_column("sec", justify="right")
    table.add_column("gates", justify="center")
    table.add_column("prod", justify="center")
    table.add_column("score", justify="right")
    for service in report.services:
        gates_ok = all(service.gates.values()) if service.gates else True
        confidence = service.confidence_counts
        table.add_row(
            service.service_id,
            str(service.endpoints),
            str(service.operations),
            f"{service.request_completeness * 100:.0f}",
            f"{service.response_completeness * 100:.0f}",
            f"{confidence.get('high', 0)}/{confidence.get('medium', 0)}/{confidence.get('low', 0)}",
            str(service.unresolved_count),
            str(service.validation_errors),
            str(service.operations_with_security),
            "[green]PASS[/green]" if gates_ok else "[red]FAIL[/red]",
            "[green]YES[/green]" if service.production_ready else "[red]NO[/red]",
            f"{service.readiness_score:.1f}",
        )
    console.print(table)
    console.print(
        f"overall readiness: [bold]{report.overall_readiness:.1f}[/bold] / 100"
        + ("  [red](strict gates failed)[/red]" if not report.strict_ok else "")
    )
    prod = "[green]YES[/green]" if report.production_ready else "[red]NO[/red]"
    console.print(f"production-ready: {prod}")
    if not report.extractors_complete:
        console.print(
            "[yellow]NOTE: a required extractor (JVM sidecar) was unavailable — "
            "output is tree-sitter-only and marked reduced-confidence.[/yellow]"
        )
    degraded = sum(s.llm_failures for s in report.services)
    if degraded:
        console.print(
            f"[yellow]NOTE: {degraded} LLM enrichment failure(s) — summaries/descriptions "
            "used deterministic fallbacks (specs are still valid; check the provider "
            "key/model for AI-authored prose).[/yellow]"
        )
    for service in report.services:
        if service.blocking_issues:
            console.print(f"[red]{service.service_id} not production-ready:[/red] " + "; ".join(service.blocking_issues))
    if report.warnings:
        console.print(f"[yellow]{len(report.warnings)} analyzer warning(s); see report file[/yellow]")
