"""LLMEnricher circuit breaker: trips on consecutive failures, resets on success.

Guards the behaviour that lets a rate-limited (429) provider keep being tried —
every success is cached, so repeated runs converge — while a truly dead provider
is still abandoned after a short run of consecutive failures.
"""

from __future__ import annotations

import tempfile

from openapi_agent.config.loader import AgentConfig
from openapi_agent.llm.base import LLMEnricher, OperationEnrichment, ProviderError
from openapi_agent.models.metadata import Confidence, Operation, Service

CONF = Confidence(level="high", reason_code="declared_annotation")


def _op(n: int) -> Operation:
    return Operation(
        method="get", operation_id=f"op{n}", handler=f"svc.H{n}.get",
        responses=[], confidence=CONF,
    )


_SVC = Service(id="svc", name="svc", language="java", framework="spring-mvc")


class _FakeProvider:
    name = "gemini"

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls = 0

    def generate_enrichment(self, prompt: str) -> OperationEnrichment:
        self.calls += 1
        if self.mode == "all_fail":
            raise ProviderError("429 RESOURCE_EXHAUSTED")
        if self.mode == "intermittent" and self.calls % 2 == 0:
            raise ProviderError("429 transient")
        return OperationEnrichment(summary="Do the thing")  # grounded: no status/path/auth

    def generate_text(self, prompt: str, max_chars: int = 1200) -> str:
        raise ProviderError("n/a")


def _enricher(mode: str) -> LLMEnricher:
    cfg = AgentConfig.model_validate(
        {"llm": {"provider": "gemini", "model": "m", "cache_dir": tempfile.mkdtemp()}}
    )
    return LLMEnricher(_FakeProvider(mode), cfg)


def test_breaker_trips_after_consecutive_failures():
    enricher = _enricher("all_fail")
    for i in range(10):
        enricher.enrich_operation(_op(i), f"/x/{i}", _SVC)
    # stops calling after 5 consecutive failures; the other 5 return fallbacks
    assert enricher.provider.calls == 5
    assert enricher._failures == 5


def test_breaker_resets_on_success():
    enricher = _enricher("intermittent")
    for i in range(20):
        enricher.enrich_operation(_op(i), f"/x/{i}", _SVC)
    # never permanently trips — every operation is attempted
    assert enricher.provider.calls == 20
    assert enricher._consecutive < 5
