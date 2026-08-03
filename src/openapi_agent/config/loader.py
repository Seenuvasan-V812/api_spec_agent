"""Merged runtime configuration.

Precedence (highest wins): CLI flags > config.yaml > .env > built-in defaults.
``AgentConfig`` is the single object the rest of the application consumes; no
module reads environment variables or config files on its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr
from ruamel.yaml import YAML

from openapi_agent.config.settings import EnvSettings

DEFAULT_EXCLUDE_DIRS: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "target",
    "build",
    "dist",
    "out",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".gradle",
    ".idea",
    ".vscode",
    "site-packages",
)

#: Directory names treated as generated/vendored and skipped by default.
DEFAULT_GENERATED_DIRS: tuple[str, ...] = ("generated", "vendor", "vendored", "third_party")


class OutputConfig(BaseModel):
    openapi_path: Path = Path("./output/openapi.yaml")
    metadata_path: Path = Path("./output/api_metadata.json")
    report_path: Path = Path("./output/readiness_report.json")
    format: Literal["yaml", "json"] = "yaml"


class JavaAnalysisConfig(BaseModel):
    sidecar_jar: Path = Path("tools/java-sidecar/target/openapi-agent-sidecar.jar")
    java_executable: str = "java"
    sidecar_timeout_seconds: int = 120
    use_spoon: bool = False


class AnalysisConfig(BaseModel):
    language: Literal["python", "java"] | None = None
    frameworks: list[str] | None = None
    services: list[str] | None = None
    exclude_dirs: list[str] = Field(default_factory=list)
    include_migrations: bool = False
    call_graph_max_depth: int = 4
    use_pyright: bool = False
    use_mypy: bool = False
    scip_index: Path | None = None
    java: JavaAnalysisConfig = Field(default_factory=JavaAnalysisConfig)

    def effective_exclude_dirs(self) -> frozenset[str]:
        names = set(DEFAULT_EXCLUDE_DIRS) | set(DEFAULT_GENERATED_DIRS) | set(self.exclude_dirs)
        if not self.include_migrations:
            names.add("migrations")
        return frozenset(names)


class LLMConfig(BaseModel):
    provider: Literal["gemini", "anthropic", "openai", "none"] = "gemini"
    model: str | None = None
    api_key: SecretStr | None = None
    timeout_seconds: int = 60
    max_retries: int = 3
    cache_dir: Path | None = Path("./.llm_cache")


class ServerEntry(BaseModel):
    url: str
    description: str | None = None
    variables: dict[str, Any] | None = None  # OpenAPI server variables


class OpenAPIInfoConfig(BaseModel):
    title: str = "My Service API"
    version: str = "1.0.0"
    servers: list[ServerEntry] = Field(
        default_factory=lambda: [ServerEntry(url="http://localhost:8080")]
    )
    contact: dict[str, str] | None = None
    license: dict[str, str] | None = None
    #: request paths beginning with any of these are service-internal
    #: (service-to-service / ops endpoints) and excluded from the public spec.
    internal_path_prefixes: list[str] = Field(
        default_factory=lambda: ["/internal", "/actuator"]
    )
    #: add conventional error responses (401/403 when secured, 404 for
    #: path-parameterized ops, a referenced ``default``) grounded in method+auth.
    #: opt-in: the default preserves the strict "emit only what is proven" contract.
    conventional_responses: bool = False
    #: annotate low-confidence operations with an ``x-openapi-agent`` vendor
    #: extension so consumers/CI can flag or hold them.
    annotate_low_confidence: bool = True


class ValidationConfig(BaseModel):
    strict: bool = False
    redocly_lint: bool = False
    spectral_lint: bool = False
    schemathesis_smoke: bool = False


class QualityConfig(BaseModel):
    """Production-readiness gates. A service failing any gate is reported
    non-production; with ``fail_on_not_production`` the run exits non-zero."""

    min_response_completeness: float = 0.75
    min_request_completeness: float = 0.75
    min_parameter_completeness: float = 0.90
    max_unresolved: int = 0
    max_llm_failures: int = 0  # LLM enrichment failures allowed before non-production
    require_descriptions: bool = False  # when True, LLM-enrichment failures block production
    require_extractors: bool = False  # missing JVM sidecar blocks production when True
    fail_on_not_production: bool = False


class ServeConfig(BaseModel):
    port: int = 8081


class AgentConfig(BaseModel):
    """Fully-merged runtime configuration."""

    project_root: Path = Path("./target-repo")
    output: OutputConfig = Field(default_factory=OutputConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    openapi: OpenAPIInfoConfig = Field(default_factory=OpenAPIInfoConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    serve: ServeConfig = Field(default_factory=ServeConfig)


def _read_yaml_config(path: Path) -> dict[str, Any]:
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a mapping at the top level")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if value is None:
            continue
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _env_layer(env: EnvSettings) -> dict[str, Any]:
    """Translate flat .env settings into the nested AgentConfig shape."""
    provider = env.llm_provider.strip().lower()
    key_by_provider = {
        "gemini": env.google_api_key,
        "openai": env.openai_api_key,
        "anthropic": env.anthropic_api_key,
    }
    model_by_provider = {
        "gemini": env.google_model,
        "openai": env.openai_model,
        "anthropic": env.anthropic_model,
    }
    api_key = key_by_provider.get(provider)
    if api_key is not None and not api_key.get_secret_value().strip():
        api_key = None
    contact = {
        k: v for k, v in (
            ("name", env.openapi_contact_name),
            ("url", env.openapi_contact_url),
            ("email", env.openapi_contact_email),
        ) if v
    } or None
    license_ = {
        k: v for k, v in (
            ("name", env.openapi_license_name),
            ("url", env.openapi_license_url),
        ) if v
    } or None
    server: dict[str, Any] = {"url": env.openapi_server_url}
    if env.openapi_server_description:
        server["description"] = env.openapi_server_description
    return {
        "project_root": env.project_root,
        "output": {
            "openapi_path": env.output_path,
            "metadata_path": env.metadata_path,
            "report_path": env.report_path,
            "format": env.output_format,
        },
        "llm": {
            "provider": provider if provider in ("gemini", "anthropic", "openai", "none") else "gemini",
            "model": model_by_provider.get(provider),
            "api_key": api_key,
            "timeout_seconds": env.llm_timeout_seconds,
            "max_retries": env.llm_max_retries,
            "cache_dir": env.llm_cache_dir,
        },
        "openapi": {
            "title": env.openapi_title,
            "version": env.openapi_version,
            "servers": [server],
            "contact": contact,
            "license": license_,
        },
        "validation": {"strict": env.strict_mode},
    }


def load_config(
    config_file: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
    env: EnvSettings | None = None,
) -> AgentConfig:
    """Build the merged configuration.

    ``cli_overrides`` uses the nested AgentConfig shape with ``None`` meaning
    "not provided" (dropped before merging).
    """
    env = env if env is not None else EnvSettings()
    layers: dict[str, Any] = _env_layer(env)

    if config_file is None:
        default_cfg = Path("config.yaml")
        if default_cfg.is_file():
            config_file = default_cfg
    if config_file is not None:
        layers = _deep_merge(layers, _read_yaml_config(Path(config_file)))

    if cli_overrides:
        layers = _deep_merge(layers, cli_overrides)

    return AgentConfig.model_validate(layers)
