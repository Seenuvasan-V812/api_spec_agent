"""JVM sidecar client (JavaParser + JavaSymbolSolver fat JAR).

The sidecar is invoked once per repository via ``subprocess`` with a hard
timeout and emits versioned JSON on stdout. It is an optional precision
booster: solver-grade resolved types for fields/methods (generics through
inheritance chains, external library types). When the JAR or a JVM is
missing the analysis continues on tree-sitter facts alone with reduced
confidence — a warning records exactly why.

Protocol (sidecar_facts_version 1.x):
    java -jar openapi-agent-sidecar.jar --repo <path> --format json
    -> {"sidecar_facts_version": "1.0.0", "types": [SidecarType...]}
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

import openapi_agent
from openapi_agent.config.loader import AgentConfig
from openapi_agent.logging_utils import get_logger

log = get_logger("analysis.java.sidecar")


class SidecarField(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    resolved_type: str = Field(alias="resolvedType", default="")


class SidecarParam(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    resolved_type: str = Field(alias="resolvedType", default="")


class SidecarMethod(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    resolved_return_type: str = Field(alias="resolvedReturnType", default="")
    parameters: list[SidecarParam] = Field(default_factory=list)


class SidecarType(BaseModel):
    model_config = ConfigDict(extra="ignore")

    qualified_name: str = Field(alias="qualifiedName")
    file: str = ""
    fields: list[SidecarField] = Field(default_factory=list)
    methods: list[SidecarMethod] = Field(default_factory=list)


class SidecarFacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sidecar_facts_version: str
    types: list[SidecarType] = Field(default_factory=list)


class SidecarResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    reason: str = ""
    types_by_qualified: dict[str, SidecarType] = Field(default_factory=dict)


def run_sidecar(config: AgentConfig, repo_root: Path) -> SidecarResult:
    jar = Path(config.analysis.java.sidecar_jar)
    if not jar.is_absolute():
        # relative to CWD first, then to the installed package root
        if not jar.is_file():
            packaged = Path(__file__).resolve().parents[4] / config.analysis.java.sidecar_jar
            if packaged.is_file():
                jar = packaged
    if not jar.is_file():
        return SidecarResult(
            available=False,
            reason=f"sidecar JAR not found at {config.analysis.java.sidecar_jar} "
            "(build it: cd tools/java-sidecar && mvnw package)",
        )
    java = shutil.which(config.analysis.java.java_executable)
    if java is None:
        return SidecarResult(
            available=False,
            reason=f"JVM not found ({config.analysis.java.java_executable!r} not on PATH)",
        )
    argv = [java, "-jar", str(jar), "--repo", str(repo_root), "--format", "json"]
    if config.analysis.java.use_spoon:
        argv.append("--spoon")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, timeout enforced
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.analysis.java.sidecar_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SidecarResult(
            available=False,
            reason=f"sidecar timed out after {config.analysis.java.sidecar_timeout_seconds}s",
        )
    except OSError as exc:
        return SidecarResult(available=False, reason=f"sidecar failed to start: {exc}")

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()[-500:]
        return SidecarResult(available=False, reason=f"sidecar exited {completed.returncode}: {tail}")

    try:
        facts = SidecarFacts.model_validate(json.loads(completed.stdout))
    except Exception as exc:  # noqa: BLE001
        return SidecarResult(available=False, reason=f"sidecar output unparseable: {exc}")

    ours = openapi_agent.SIDECAR_FACTS_VERSION.split(".")[0]
    theirs = facts.sidecar_facts_version.split(".")[0]
    if ours != theirs:
        # hard refusal on major mismatch: mis-parsing facts is worse than none
        return SidecarResult(
            available=False,
            reason=f"sidecar facts version {facts.sidecar_facts_version} incompatible "
            f"with supported {openapi_agent.SIDECAR_FACTS_VERSION}",
        )
    log.info("sidecar provided %d resolved types", len(facts.types))
    return SidecarResult(
        available=True,
        types_by_qualified={t.qualified_name: t for t in facts.types},
    )
