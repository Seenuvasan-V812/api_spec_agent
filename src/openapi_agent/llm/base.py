"""LLM enrichment layer.

The enricher improves ONLY description-class text: operation summaries,
descriptions, tags, parameter/response descriptions, and the API overview.
It can never add or modify contract facts — the builder consumes nothing else
from it, model output is schema-validated (Pydantic) and grounded against the
operation metadata, and a *transient* per-call failure falls back to
deterministic templates so one API hiccup never aborts a run.

CLI generation strictly requires a usable LLM: the ``analyze``/``run``/
``generate`` commands reject a missing provider or key up front, and
``get_enricher`` raises ``ProviderError`` when a real provider is selected
without a usable key. ``provider="none"`` remains available only for internal
and test use, where it short-circuits to templates.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from openapi_agent.config.loader import AgentConfig
from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import Operation, Parameter, Service

log = get_logger("llm")

#: bump when the prompt format changes — part of the cache key
PROMPT_VERSION = "1"


class OperationEnrichment(BaseModel):
    """The only thing an LLM may contribute for an operation."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=300)
    description: Optional[str] = Field(default=None, max_length=4000)
    tags: Optional[list[str]] = None
    response_descriptions: dict[str, str] = Field(default_factory=dict)
    request_body_description: Optional[str] = None
    parameter_descriptions: dict[str, str] = Field(default_factory=dict)


class ProviderError(RuntimeError):
    pass


class LLMProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def generate_enrichment(self, prompt: str) -> OperationEnrichment:
        """Return schema-valid enrichment or raise ProviderError."""

    @abstractmethod
    def generate_text(self, prompt: str, max_chars: int = 1200) -> str:
        """Free-text completion (API overview); raise ProviderError on failure."""


_GENERIC_HANDLER_NAMES = {
    "get", "post", "put", "delete", "patch", "handle", "index", "list", "apply", "call", "execute",
}
_HTTP_VERB_PHRASE = {
    "get": "Get", "post": "Create", "put": "Update", "patch": "Update",
    "delete": "Delete", "head": "Check", "options": "Get options for",
}


def _split_identifier(name: str) -> list[str]:
    """camelCase / snake_case / kebab-case → lower-cased words."""
    name = re.sub(r"[_\-]+", " ", name)
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return [w.lower() for w in name.split() if w]


def _derive_summary(operation: Operation, path: str) -> str:
    """A human summary from source names, never a raw 'METHOD /path' placeholder.

    Prefers the handler method name (``forgotPassword`` → "Forgot password");
    falls back to the HTTP verb + the last concrete path segment.
    """
    handler = (operation.handler or "").rsplit(".", 1)[-1]
    words = _split_identifier(handler)
    if words and words[0] not in _GENERIC_HANDLER_NAMES:
        return " ".join(words).capitalize()
    # fall back to verb + resource from the path
    segments = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
    resource = " ".join(_split_identifier(segments[-1])) if segments else "resource"
    verb = _HTTP_VERB_PHRASE.get(operation.method.lower(), operation.method.upper())
    templated = path.rstrip("/").endswith("}")
    if operation.method.lower() == "get" and not templated and segments:
        return f"List {resource}".strip()
    return f"{verb} {resource}".strip()


_BASE_SEG_RE = re.compile(r"^(?:api|v\d+|internal|actuator)$", re.IGNORECASE)


def _derive_overview(service: Service) -> str | None:
    """A deterministic, grounded API overview from the service name and the
    resources present in its paths — so ``info.description`` is populated even
    when the LLM is unavailable."""
    resources: list[str] = []
    seen: set[str] = set()
    for endpoint in service.endpoints:
        segments = [s for s in endpoint.path.strip("/").split("/") if s and not s.startswith("{")]
        i = 0
        while i < len(segments) and _BASE_SEG_RE.match(segments[i]):
            i += 1
        if i < len(segments):
            resource = segments[i].replace("-", " ").replace("_", " ")
            if resource not in seen:
                seen.add(resource)
                resources.append(resource)
    name = (service.name or service.id).replace("-", " ").replace("_", " ").strip()
    if not name:
        return None
    if resources:
        return f"API for the {name}. Provides operations over: {', '.join(sorted(resources))}."
    return f"API for the {name}."


class TemplateEnricher:
    """Fully deterministic enrichment: the transient-failure fallback and the
    internal ``provider="none"`` path (not selectable from the CLI)."""

    def enrich_operation(self, operation: Operation, path: str, service: Service) -> OperationEnrichment:
        summary = operation.summary_hint or _derive_summary(operation, path)
        return OperationEnrichment(
            summary=summary[:300],
            description=operation.description_hint or None,
            tags=operation.tags_hint or None,
            response_descriptions={
                v.status: v.description_hint
                for v in operation.responses
                if v.description_hint
            },
        )

    def api_overview(self, service: Service) -> str | None:
        return service.description_hint or _derive_overview(service)

    def tag_description(self, name: str, service: Service) -> str | None:
        return None

    def parameter_description(self, parameter: Parameter, operation: Operation, path: str) -> str | None:
        return parameter.description_hint


class LLMEnricher(TemplateEnricher):
    """Provider-backed enrichment with caching, grounding, and fallback."""

    def __init__(self, provider: LLMProvider, config: AgentConfig) -> None:
        self.provider = provider
        self.config = config
        from openapi_agent.llm.cache import ResponseCache

        self.cache = ResponseCache(config.llm.cache_dir)
        self._failures = 0  # total failures this run (reported)
        self._consecutive = 0  # consecutive failures (drives the circuit breaker)
        self._param_notes: dict[tuple[str, str], dict[str, str]] = {}

    #: consecutive provider failures before we stop calling for the rest of the run
    _BREAKER_THRESHOLD = 5

    def enrich_operation(self, operation: Operation, path: str, service: Service) -> OperationEnrichment:
        fallback = super().enrich_operation(operation, path, service)
        # Circuit breaker trips only on a *run* of consecutive failures, so a
        # provider that is merely rate-limited (some calls succeed) keeps being
        # tried — and every success is cached, so repeated runs fill in the rest.
        if self._consecutive >= self._BREAKER_THRESHOLD:
            return fallback
        from openapi_agent.llm.grounding import compact_operation_payload, is_grounded

        payload = compact_operation_payload(operation, path, service)
        prompt = _OPERATION_PROMPT.format(payload=payload)
        cache_key = self.cache.key(self.provider.name, self.config.llm.model or "", PROMPT_VERSION, payload)

        cached = self.cache.get(cache_key, OperationEnrichment)
        if cached is not None:
            enrichment = cached
        else:
            try:
                enrichment = self.provider.generate_enrichment(prompt)
            except Exception as exc:  # noqa: BLE001 - provider failure => deterministic output
                self._failures += 1
                self._consecutive += 1
                log.warning("LLM enrichment failed for %s %s: %s", operation.method, path, exc)
                return fallback
            self.cache.put(cache_key, enrichment)
        self._consecutive = 0  # a usable response (fresh or cached) resets the breaker

        ok, reason = is_grounded(enrichment, operation, path, service)
        if not ok:
            log.warning("LLM output rejected (ungrounded: %s) for %s %s", reason, operation.method, path)
            return fallback
        self._param_notes[(operation.operation_id, path)] = enrichment.parameter_descriptions
        # merge: LLM text wins, but hints survive where the model said nothing
        merged_responses = dict(fallback.response_descriptions)
        merged_responses.update(enrichment.response_descriptions)
        return OperationEnrichment(
            summary=enrichment.summary or fallback.summary,
            description=enrichment.description or fallback.description,
            tags=enrichment.tags or fallback.tags,
            response_descriptions=merged_responses,
            request_body_description=enrichment.request_body_description,
            parameter_descriptions=enrichment.parameter_descriptions,
        )

    def parameter_description(self, parameter: Parameter, operation: Operation, path: str) -> str | None:
        notes = self._param_notes.get((operation.operation_id, path), {})
        return notes.get(parameter.name) or parameter.description_hint

    def api_overview(self, service: Service) -> str | None:
        base = super().api_overview(service)
        if self._consecutive >= self._BREAKER_THRESHOLD:
            return base
        endpoint_list = "\n".join(
            f"- {op.method.upper()} {ep.path}: {op.summary_hint or ''}"
            for ep in service.endpoints
            for op in ep.operations
        )
        prompt = _OVERVIEW_PROMPT.format(
            name=service.name, hint=base or "", endpoints=endpoint_list[:6000]
        )
        cache_key = self.cache.key(self.provider.name, self.config.llm.model or "", PROMPT_VERSION, prompt)
        cached_text = self.cache.get_text(cache_key)
        if cached_text is not None:
            self._consecutive = 0
            return cached_text or base
        try:
            text = self.provider.generate_text(prompt).strip()
        except Exception as exc:  # noqa: BLE001
            self._failures += 1
            self._consecutive += 1
            log.warning("LLM overview failed: %s", exc)
            return base
        self._consecutive = 0
        self.cache.put_text(cache_key, text)
        return text or base


_OPERATION_PROMPT = """\
You are documenting one HTTP API operation for consumers of the API.
Using ONLY the facts in the JSON below, write consumer-facing text.
Rules:
- Do not invent endpoints, fields, parameters, status codes, headers, or auth.
- Mention only names present in the metadata.
- summary: one imperative sentence, max 12 words, no trailing period needed.
- description: 1-3 sentences of consumer guidance; omit if nothing useful to add.
- tags: 1-2 short lowercase resource tags grounded in the path or existing tags.
- response_descriptions: map status code -> one short sentence.
- parameter_descriptions: map parameter name -> one short sentence.

Operation metadata:
{payload}
"""

_OVERVIEW_PROMPT = """\
Write a 2-4 sentence overview for an API named "{name}" for its documentation
home page. Existing description hint: "{hint}". The API has these operations:
{endpoints}

Do not invent capabilities that are not implied by the operations listed.
Return plain text only.
"""


def get_enricher(config: AgentConfig):
    """Factory honoring provider config; import provider SDKs lazily."""
    provider_name = config.llm.provider
    if provider_name == "none":
        # Explicit opt-out; retained for internal/test use only (the CLI rejects it).
        return TemplateEnricher()
    api_key = config.llm.api_key.get_secret_value() if config.llm.api_key else None
    if not api_key or api_key.startswith("your-"):
        raise ProviderError(
            f"LLM is required but no usable {provider_name} API key is configured. "
            f"Set the matching key in .env (e.g. GOOGLE_API_KEY for gemini)."
        )
    try:
        if provider_name == "gemini":
            from openapi_agent.llm.gemini import GeminiProvider

            provider: LLMProvider = GeminiProvider(config)
        elif provider_name == "anthropic":
            from openapi_agent.llm.anthropic_ import AnthropicProvider

            provider = AnthropicProvider(config)
        elif provider_name == "openai":
            from openapi_agent.llm.openai_ import OpenAIProvider

            provider = OpenAIProvider(config)
        else:  # pragma: no cover - config validation prevents this
            return TemplateEnricher()
    except Exception as exc:  # noqa: BLE001 - missing SDK etc.
        log.warning("LLM provider %s unavailable (%s); using templates", provider_name, exc)
        return TemplateEnricher()
    return LLMEnricher(provider, config)
