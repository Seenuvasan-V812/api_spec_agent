"""Java analysis context: tree-sitter index + optional sidecar facts."""

from __future__ import annotations

from dataclasses import dataclass, field

from openapi_agent.analysis.base import AnalysisContext
from openapi_agent.analysis.java.sidecar_client import SidecarResult, run_sidecar
from openapi_agent.analysis.java.ts_scanner import JavaIndex, build_java_index
from openapi_agent.logging_utils import get_logger

log = get_logger("analysis.java.context")


@dataclass
class JavaAnalysisContext(AnalysisContext):
    index: JavaIndex = field(default_factory=JavaIndex)
    sidecar: SidecarResult = field(default_factory=lambda: SidecarResult(available=False))


def build_java_context(base: AnalysisContext) -> JavaAnalysisContext:
    ctx = JavaAnalysisContext(
        repo_root=base.repo_root,
        repo_facts=base.repo_facts,
        config=base.config,
        warnings=base.warnings,
        registry=base.registry,
        extras=base.extras,
    )
    ctx.index = build_java_index(base.repo_root, base.repo_facts.java_files)
    for failed in ctx.index.files_failed:
        ctx.warnings.parse_failure(failed, "unreadable java source")
    ctx.sidecar = run_sidecar(base.config, base.repo_root)
    if not ctx.sidecar.available:
        ctx.warnings.emit(
            "W401",
            f"JVM sidecar unavailable ({ctx.sidecar.reason}); "
            "tree-sitter-only extraction, affected contracts marked reduced confidence",
        )
    return ctx
