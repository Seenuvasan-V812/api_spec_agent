"""Google Gemini provider (default) — official ``google-genai`` SDK.

JSON output is requested via ``response_mime_type=application/json`` and the
result is validated with Pydantic. We deliberately do NOT pass a
``response_schema`` built from :class:`OperationEnrichment`: that model has open
string maps (``response_descriptions`` / ``parameter_descriptions``) which the
SDK renders as ``additionalProperties``, and the Gemini *Developer API* rejects
``additionalProperties`` outright. Validating the returned JSON ourselves keeps
the provider working across schemas and API modes."""

from __future__ import annotations

import json

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from openapi_agent.config.loader import AgentConfig
from openapi_agent.llm.base import LLMProvider, OperationEnrichment, ProviderError
from openapi_agent.logging_utils import get_logger

log = get_logger("llm.gemini")


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, config: AgentConfig) -> None:
        from google import genai

        api_key = config.llm.api_key.get_secret_value() if config.llm.api_key else None
        if not api_key:
            raise ProviderError("GOOGLE_API_KEY is not configured")
        self.model = config.llm.model or "gemini-2.5-flash"
        self.timeout_ms = config.llm.timeout_seconds * 1000
        self.max_retries = config.llm.max_retries
        self._client = genai.Client(
            api_key=api_key, http_options={"timeout": self.timeout_ms}
        )

    def _retrying(self):
        return retry(
            stop=stop_after_attempt(max(1, self.max_retries)),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )

    def generate_enrichment(self, prompt: str) -> OperationEnrichment:
        schema_hint = json.dumps(OperationEnrichment.model_json_schema())
        full_prompt = f"{prompt}\n\nReturn ONLY a JSON object matching this schema:\n{schema_hint}"

        @self._retrying()
        def _call() -> OperationEnrichment:
            response = self._client.models.generate_content(
                model=self.model,
                contents=full_prompt,
                config={"response_mime_type": "application/json", "temperature": 0.2},
            )
            text = getattr(response, "text", None)
            if not text:
                raise ProviderError("empty Gemini response")
            return OperationEnrichment.model_validate_json(_strip_fences(text))

        try:
            return _call()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"gemini enrichment failed: {type(exc).__name__}: {str(exc)[:200]}") from exc

    def generate_text(self, prompt: str, max_chars: int = 1200) -> str:
        @self._retrying()
        def _call() -> str:
            response = self._client.models.generate_content(
                model=self.model, contents=prompt, config={"temperature": 0.3}
            )
            text = getattr(response, "text", None)
            if not text:
                raise ProviderError("empty Gemini response")
            return text[:max_chars]

        try:
            return _call()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"gemini text failed: {type(exc).__name__}: {str(exc)[:200]}") from exc


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()
