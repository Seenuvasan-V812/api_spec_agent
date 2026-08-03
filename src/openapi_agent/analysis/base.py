"""Framework adapter contract + plugin registry.

Adapters are emit-side uniform (they produce the neutral models from
``openapi_agent.models``) and consume-side language-specific (each adapter
declares which :class:`AnalysisContext` subclass it needs; the orchestrator
builds it). Registration is entry-point based: third-party packages add
adapters under the ``openapi_agent.adapters`` group with zero core changes.
"""

from __future__ import annotations

import importlib.metadata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from openapi_agent.config.loader import AgentConfig
from openapi_agent.detection.repo import RepoFacts
from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import (
    AnalysisWarning,
    Evidence,
    Operation,
    ReasonCode,
    Service,
)
from openapi_agent.models.registry import SchemaRegistryBuilder

log = get_logger("analysis.base")

ADAPTER_ENTRYPOINT_GROUP = "openapi_agent.adapters"

#: Activation threshold for ``can_handle`` scores.
DETECTION_THRESHOLD = 0.5


class DetectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    rationale: str = ""


class RouteRef(BaseModel):
    """Cheap handle produced by route discovery; enough to locate, not a contract."""

    model_config = ConfigDict(extra="forbid")

    service_hint: str
    raw_path: str
    methods: list[str]
    handler_symbol: str
    file: str
    start_line: int = 1
    kind: Literal[
        "decorator",
        "annotation",
        "functional",
        "urlconf",
        "method_view",
        "viewset_action",
        "router_registration",
    ] = "decorator"


class UnresolvedSite(BaseModel):
    """Logical unresolved-contract record; the pipeline converts it to a JSON
    Pointer into the final document once positions are known."""

    model_config = ConfigDict(extra="forbid")

    service_id: str
    path: str  # normalized endpoint path
    method: str
    site: str  # pointer suffix relative to the operation, e.g. "request_body/content/application~1json/schema"
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


class OperationExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_path: str
    raw_path: str
    operation: Operation
    unresolved: list[UnresolvedSite] = Field(default_factory=list)


class WarningSink:
    """The single mutable channel adapters may write diagnostics to."""

    def __init__(self) -> None:
        self._warnings: dict[tuple, AnalysisWarning] = {}
        self.files_failed_parse: set[str] = set()

    def emit(
        self,
        code: str,
        message: str,
        file: str | None = None,
        start_line: int | None = None,
        service_id: str | None = None,
    ) -> None:
        warning = AnalysisWarning(
            code=code, message=message, file=file, start_line=start_line, service_id=service_id
        )
        self._warnings[(code, warning.file, start_line, message, service_id)] = warning

    def parse_failure(self, file: str, message: str, line: int | None = None) -> None:
        self.files_failed_parse.add(file)
        self.emit("W101", f"parse error: {message}", file=file, start_line=line)

    @property
    def warnings(self) -> list[AnalysisWarning]:
        return list(self._warnings.values())


@dataclass
class AnalysisContext:
    """Shared analysis state passed to adapters. Language-specific subclasses
    add their toolchains; ``repo_root`` is internal and never serialized."""

    repo_root: Path
    repo_facts: RepoFacts
    config: AgentConfig
    warnings: WarningSink
    registry: SchemaRegistryBuilder
    extras: dict = field(default_factory=dict)

    def rel(self, path: Path | str) -> str:
        p = Path(path)
        try:
            return p.resolve().relative_to(self.repo_root.resolve()).as_posix()
        except ValueError:
            return p.as_posix()


class FrameworkAdapter(ABC):
    """Contract every framework adapter implements.

    Contract-level extraction failures must degrade (empty schema + low
    confidence + warning), never raise: one broken endpoint must not sink the
    document.
    """

    name: ClassVar[str]
    language: ClassVar[Literal["python", "java"]]

    @abstractmethod
    def can_handle(self, facts: RepoFacts) -> DetectionResult:
        """Pure scoring from the pre-scan. No parsing."""

    @abstractmethod
    def discover_services(self, ctx: AnalysisContext) -> list[Service]:
        """Service shells (id, name, root, base paths, security schemes); endpoints empty."""

    @abstractmethod
    def discover_routes(self, ctx: AnalysisContext, service: Service) -> list[RouteRef]:
        """Enumerate every route registration mechanism the framework supports."""

    @abstractmethod
    def extract_operation(
        self, ctx: AnalysisContext, service: Service, route: RouteRef
    ) -> list[OperationExtraction]:
        """Full contract extraction for one route (one item per HTTP method)."""


class AdapterLoadError(RuntimeError):
    pass


def load_adapters(only: list[str] | None = None) -> list[FrameworkAdapter]:
    """Instantiate registered adapters in deterministic (name-sorted) order."""
    entry_points = importlib.metadata.entry_points(group=ADAPTER_ENTRYPOINT_GROUP)
    seen: dict[str, str] = {}
    adapters: list[FrameworkAdapter] = []
    for entry_point in sorted(entry_points, key=lambda e: e.name):
        if only is not None and entry_point.name not in only:
            continue
        if entry_point.name in seen:
            raise AdapterLoadError(
                f"duplicate adapter name {entry_point.name!r}: "
                f"{seen[entry_point.name]} vs {entry_point.value}"
            )
        seen[entry_point.name] = entry_point.value
        try:
            cls = entry_point.load()
        except Exception as exc:  # noqa: BLE001
            raise AdapterLoadError(f"failed to load adapter {entry_point.name!r}: {exc}") from exc
        if not (isinstance(cls, type) and issubclass(cls, FrameworkAdapter)):
            raise AdapterLoadError(f"adapter {entry_point.name!r} is not a FrameworkAdapter subclass")
        adapters.append(cls())
    if only is not None:
        missing = set(only) - set(seen)
        if missing:
            raise AdapterLoadError(f"unknown adapter name(s): {sorted(missing)}")
    return adapters


def select_adapters(
    adapters: list[FrameworkAdapter],
    facts: RepoFacts,
    threshold: float = DETECTION_THRESHOLD,
) -> list[tuple[FrameworkAdapter, DetectionResult]]:
    scored = [(adapter, adapter.can_handle(facts)) for adapter in adapters]
    activated = [(a, d) for a, d in scored if d.score >= threshold]
    activated.sort(key=lambda t: (-t[1].score, t[0].name))
    for adapter, detection in activated:
        log.info("adapter %s activated (score=%.2f): %s", adapter.name, detection.score, detection.rationale)
    return activated
