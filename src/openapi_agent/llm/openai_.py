"""OpenAI provider — official ``openai`` SDK with JSON response format."""

from __future__ import annotations

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from openapi_agent.config.loader import AgentConfig
from openapi_agent.llm.base import LLMProvider, OperationEnrichment, ProviderError
from openapi_agent.logging_utils import get_logger

log = get_logger("llm.openai")


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, config: AgentConfig) -> None:
        import openai

        api_key = config.llm.api_key.get_secret_value() if config.llm.api_key else None
        if not api_key:
            raise ProviderError("OPENAI_API_KEY is not configured")
        self.model = config.llm.model or "gpt-4o-mini"
        self.max_retries = config.llm.max_retries
        self._client = openai.OpenAI(
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

    def generate_enrichment(self, prompt: str) -> OperationEnrichment:
        @self._retrying()
        def _call() -> OperationEnrichment:
            completion = self._client.chat.completions.create(
                model=self.model,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": "Return a single JSON object with keys: summary, description, "
                        "tags, response_descriptions, request_body_description, parameter_descriptions.",
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            text = completion.choices[0].message.content or ""
            return OperationEnrichment.model_validate_json(text)

        try:
            return _call()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"openai enrichment failed: {type(exc).__name__}: {str(exc)[:200]}") from exc

    def generate_text(self, prompt: str, max_chars: int = 1200) -> str:
        @self._retrying()
        def _call() -> str:
            completion = self._client.chat.completions.create(
                model=self.model,
                temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
            )
            return (completion.choices[0].message.content or "")[:max_chars]

        try:
            return _call()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"openai text failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
