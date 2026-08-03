"""Phase-2 orchestrator: metadata JSON → per-service OpenAPI documents.

One document per service; multi-service repositories additionally get a
service catalog (``<output-stem>.catalog.json``) mapping service ids to spec
paths.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from rich.console import Console

from openapi_agent.config.loader import AgentConfig
from openapi_agent.llm.base import get_enricher
from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import MetadataDocument
from openapi_agent.openapi.builder import build_openapi_document
from openapi_agent.openapi.validators import (
    ValidationOutcome,
    run_optional_layers,
    validate_document_dict,
)
from openapi_agent.openapi.writer import write_document

log = get_logger("openapi.generator")


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: str
    output_path: str
    endpoints: int
    operations: int
    validation: ValidationOutcome
    llm_provider: str
    llm_failures: int = 0


def load_metadata(path: Path) -> MetadataDocument:
    text = Path(path).read_text(encoding="utf-8")
    return MetadataDocument.model_validate_json(text)


def _service_output_path(base: Path, service_id: str, multi: bool, fmt: str) -> Path:
    base = Path(base)
    suffix = ".json" if fmt == "json" else (base.suffix if base.suffix in (".yaml", ".yml") else ".yaml")
    if not multi:
        return base.with_suffix(suffix)
    return base.with_name(f"{base.stem}.{service_id}{suffix}")


def run_generation(config: AgentConfig, console: Console | None = None) -> list[GenerationResult]:
    console = console or Console()
    metadata = load_metadata(config.output.metadata_path)
    services = metadata.services
    if config.analysis.services:
        services = [s for s in services if s.id in config.analysis.services]
    if not services:
        raise RuntimeError(
            "metadata contains no services to generate from "
            f"({config.output.metadata_path}); run `analyze` first"
        )

    enricher = get_enricher(config)
    multi = len(services) > 1
    results: list[GenerationResult] = []

    for service in services:
        document = build_openapi_document(metadata, service, config, enricher)
        output_path = _service_output_path(
            config.output.openapi_path, service.id, multi, config.output.format
        )
        final_path = write_document(document, output_path, config.output.format)
        outcome = validate_document_dict(document, config, metadata, service)
        run_optional_layers(final_path, config, outcome)
        results.append(
            GenerationResult(
                service_id=service.id,
                output_path=str(final_path),
                endpoints=len(service.endpoints),
                operations=sum(len(e.operations) for e in service.endpoints),
                validation=outcome,
                llm_provider=config.llm.provider,
                llm_failures=getattr(enricher, "_failures", 0),
            )
        )
        status = "[green]valid[/green]" if outcome.ok else "[red]INVALID[/red]"
        console.print(f"[dim]{service.id}: {final_path} {status}[/dim]")

    if multi:
        catalog_path = Path(config.output.openapi_path).with_name(
            Path(config.output.openapi_path).stem + ".catalog.json"
        )
        catalog = {
            "services": [
                {"id": r.service_id, "spec": r.output_path.replace("\\", "/")} for r in results
            ]
        }
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
        console.print(f"[dim]service catalog: {catalog_path}[/dim]")
    return results
