"""Environment-backed settings (.env / process environment).

Only environment-shaped values live here; the merged runtime configuration is
:class:`openapi_agent.config.loader.AgentConfig`.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    """Mirror of ``.env.example``. All fields optional with safe defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Target repository
    project_root: str = Field(default="./target-repo", alias="PROJECT_ROOT")

    # Output paths
    output_path: str = Field(default="./output/openapi.yaml", alias="OUTPUT_PATH")
    metadata_path: str = Field(default="./output/api_metadata.json", alias="METADATA_PATH")
    report_path: str = Field(default="./output/readiness_report.json", alias="REPORT_PATH")

    # LLM provider
    llm_provider: str = Field(default="gemini", alias="LLM_PROVIDER")
    google_api_key: SecretStr | None = Field(default=None, alias="GOOGLE_API_KEY")
    google_model: str = Field(default="gemini-2.5-flash", alias="GOOGLE_MODEL")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-5", alias="ANTHROPIC_MODEL")

    # LLM runtime controls
    llm_timeout_seconds: int = Field(default=60, alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=3, alias="LLM_MAX_RETRIES")
    llm_cache_dir: str = Field(default="./.llm_cache", alias="LLM_CACHE_DIR")

    # OpenAPI document metadata
    openapi_title: str = Field(default="My Service API", alias="OPENAPI_TITLE")
    openapi_version: str = Field(default="1.0.0", alias="OPENAPI_VERSION")
    openapi_server_url: str = Field(
        default="http://localhost:8080/api/v1", alias="OPENAPI_SERVER_URL"
    )
    openapi_server_description: str | None = Field(default=None, alias="OPENAPI_SERVER_DESCRIPTION")

    # info.contact / info.license (optional; omitted when unset)
    openapi_contact_name: str | None = Field(default=None, alias="OPENAPI_CONTACT_NAME")
    openapi_contact_url: str | None = Field(default=None, alias="OPENAPI_CONTACT_URL")
    openapi_contact_email: str | None = Field(default=None, alias="OPENAPI_CONTACT_EMAIL")
    openapi_license_name: str | None = Field(default=None, alias="OPENAPI_LICENSE_NAME")
    openapi_license_url: str | None = Field(default=None, alias="OPENAPI_LICENSE_URL")

    # Behavior flags
    strict_mode: bool = Field(default=False, alias="STRICT_MODE")
    output_format: str = Field(default="yaml", alias="OUTPUT_FORMAT")
