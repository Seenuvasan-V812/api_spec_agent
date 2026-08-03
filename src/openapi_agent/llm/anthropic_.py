"""Anthropic Claude provider — official ``anthropic`` SDK."""

from __future__ import annotations

import json

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from openapi_agent.config.loader import AgentConfig
from openapi_agent.llm.base import LLMProvider, OperationEnrichment, ProviderError
from openapi_agent.logging_utils import get_logger

log = get_logger("llm.anthropic")


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, config: AgentConfig) -> None:
        import anthropic

        api_key = config.llm.api_key.get_secret_value() if config.llm.api_key else None
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not configured")
        self.model = config.llm.model or "claude-sonnet-4-5"
        self.max_retries = config.llm.max_retries
        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=float(config.llm.timeout_seconds),
            max_retries=0,  # tenacity owns retries
        )

    def _retrying(self):
        return retry(
            stop=stop_after_attempt(max(1, self.max_retries)),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )

    def _complete(self, prompt: str, force_json: bool) -> str:
        system = (
            "Respond with a single JSON object matching the requested fields. "
            "No markdown fences, no commentary."
            if force_json
            else "Respond with plain text only."
        )
        message = self._client.messages.create(
            model=self.model,
            max_tokens=1500,
            temperature=0.2,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [block.text for block in message.content if getattr(block, "type", "") == "text"]
        return "".join(parts).strip()

    def generate_enrichment(self, prompt: str) -> OperationEnrichment:
        schema_hint = json.dumps(OperationEnrichment.model_json_schema())
        full_prompt = f"{prompt}\n\nReturn JSON matching this schema:\n{schema_hint}"

        @self._retrying()
        def _call() -> OperationEnrichment:
            text = self._complete(full_prompt, force_json=True)
            text = _strip_fences(text)
            return OperationEnrichment.model_validate_json(text)

        try:
            return _call()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"anthropic enrichment failed: {type(exc).__name__}: {str(exc)[:200]}") from exc

    def generate_text(self, prompt: str, max_chars: int = 1200) -> str:
        @self._retrying()
        def _call() -> str:
            return self._complete(prompt, force_json=False)[:max_chars]

        try:
            return _call()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"anthropic text failed: {type(exc).__name__}: {str(exc)[:200]}") from exc


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()
