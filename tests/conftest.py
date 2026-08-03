"""Shared test helpers: run the full pipeline against a fixture repo."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ruamel.yaml import YAML

FIXTURES = Path(__file__).parent / "fixtures"


def make_config(fixture: str, tmp_path: Path, **overrides):
    from openapi_agent.config.loader import AgentConfig

    values = {
        "project_root": FIXTURES / fixture,
        "output": {
            "metadata_path": tmp_path / "meta.json",
            "openapi_path": tmp_path / "openapi.yaml",
            "report_path": tmp_path / "report.json",
        },
        "llm": {"provider": "none"},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and key in values:
            values[key].update(value)
        else:
            values[key] = value
    return AgentConfig.model_validate(values)


def run_pipeline(fixture: str, tmp_path: Path, **overrides):
    """analyze + generate; returns (metadata_dict, docs_by_service, results)."""
    from openapi_agent.analysis.pipeline import run_analysis
    from openapi_agent.openapi.generator import run_generation

    config = make_config(fixture, tmp_path, **overrides)
    run_analysis(config)
    results = run_generation(config)
    metadata = json.loads(config.output.metadata_path.read_text(encoding="utf-8"))
    docs = {}
    for result in results:
        path = Path(result.output_path)
        if path.suffix == ".json":
            docs[result.service_id] = json.loads(path.read_text(encoding="utf-8"))
        else:
            docs[result.service_id] = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    return metadata, docs, results


def doc_operations(doc: dict) -> set[tuple[str, str]]:
    methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    return {
        (path, method)
        for path, item in (doc.get("paths") or {}).items()
        for method in item
        if method in methods
    }


@pytest.fixture(autouse=True)
def _isolate_astroid():
    """astroid caches modules globally; clear between tests for isolation."""
    yield
    import astroid

    astroid.MANAGER.clear_cache()
