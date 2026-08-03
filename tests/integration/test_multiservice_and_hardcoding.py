"""Microservice repos, no-hardcoding proof, and provider-failure resilience."""

import json
from pathlib import Path

from tests.conftest import doc_operations, make_config, run_pipeline


def test_microservices_two_specs_and_catalog(tmp_path):
    metadata, docs, results = run_pipeline("microservices", tmp_path)
    assert {r.service_id for r in results} == {"catalog", "checkout"}
    assert all(r.validation.ok for r in results)
    assert doc_operations(docs["catalog"]) == {("/products", "get"), ("/products/{sku}", "get")}
    assert doc_operations(docs["checkout"]) == {("/checkout", "post")}
    catalog_file = json.loads((tmp_path / "openapi.catalog.json").read_text(encoding="utf-8"))
    assert len(catalog_file["services"]) == 2


def test_service_filter_limits_generation(tmp_path):
    _metadata, docs, results = run_pipeline(
        "microservices", tmp_path, analysis={"services": ["catalog"]}
    )
    assert {r.service_id for r in results} == {"catalog"}


def test_no_hardcoded_behavior_across_fixtures(tmp_path):
    """The same pipeline must produce fixture-specific results purely from
    input: run against two different repos and verify nothing leaks across."""
    _m1, docs1, _r1 = run_pipeline("fastapi_app", tmp_path / "one")
    _m2, docs2, _r2 = run_pipeline("flask_app", tmp_path / "two")
    ops1 = doc_operations(next(iter(docs1.values())))
    ops2 = doc_operations(next(iter(docs2.values())))
    assert ops1 != ops2
    # no cross-contamination: petstore models never appear in the flask doc
    blob2 = json.dumps(docs2)
    assert "OrderItem" not in blob2 and "PetStatus" not in blob2


def test_no_fixture_strings_in_source_tree():
    """No endpoint path, model name, or fixture-specific behavior is hardcoded
    in the tool's source."""
    src = Path(__file__).parents[2] / "src"
    needles = ("petstore", "bookstore", "/api/v1/pets", "PetCreate", "CreateBookRequest", "Watchlist")
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for needle in needles:
            assert needle.lower() not in text, f"{needle} found in {path}"


def test_provider_failure_still_yields_valid_document(tmp_path, monkeypatch):
    """A dead LLM provider must never break generation."""
    from pydantic import SecretStr

    from openapi_agent.analysis.pipeline import run_analysis
    from openapi_agent.llm.base import LLMEnricher, LLMProvider, ProviderError
    from openapi_agent.openapi import generator as generator_module
    from openapi_agent.openapi.generator import run_generation

    class DeadProvider(LLMProvider):
        name = "dead"

        def generate_enrichment(self, prompt):
            raise ProviderError("provider exploded")

        def generate_text(self, prompt, max_chars=1200):
            raise ProviderError("provider exploded")

    config = make_config("fastapi_app", tmp_path, llm={"provider": "gemini",
                                                       "api_key": SecretStr("sk-fake-key-123456"),
                                                       "cache_dir": None})
    monkeypatch.setattr(
        generator_module, "get_enricher", lambda cfg: LLMEnricher(DeadProvider(), cfg)
    )
    run_analysis(config)
    results = run_generation(config)
    (result,) = results
    assert result.validation.ok, [m.text for m in result.validation.messages]
    assert all(result.validation.gates.values())


def test_secrets_never_reach_outputs(tmp_path, monkeypatch):
    secret = "sk-super-secret-key-abcdef123456"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    _metadata, docs, _results = run_pipeline("fastapi_app", tmp_path)
    for artifact in tmp_path.rglob("*"):
        if artifact.is_file():
            assert secret not in artifact.read_text(encoding="utf-8", errors="replace")
