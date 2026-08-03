"""Typer CLI: analyze | generate | run | validate | report | serve."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console

from openapi_agent.config.loader import AgentConfig, load_config
from openapi_agent.logging_utils import configure_logging, get_logger, register_secret

app = typer.Typer(
    name="openapi-agent",
    help="Generate validated OpenAPI 3.1 documentation from Java/Python repositories via static analysis.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
console = Console()
log = get_logger("cli")

ConfigOpt = Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml")]
LanguageOpt = Annotated[Optional[str], typer.Option("--language", help="Force language: python|java")]
FrameworkOpt = Annotated[Optional[list[str]], typer.Option("--framework", help="Force framework adapter(s)")]
ServiceOpt = Annotated[Optional[list[str]], typer.Option("--service", help="Only analyze these service ids")]
ProviderOpt = Annotated[Optional[str], typer.Option("--provider", help="LLM provider: gemini|anthropic|openai")]
ModelOpt = Annotated[Optional[str], typer.Option("--model", help="LLM model name override")]
MetadataOutOpt = Annotated[Optional[Path], typer.Option("--metadata-output", help="Metadata JSON output path")]
OpenapiOutOpt = Annotated[Optional[Path], typer.Option("--openapi-output", help="OpenAPI document output path")]
FormatOpt = Annotated[Optional[str], typer.Option("--format", help="Output format: yaml|json")]
StrictOpt = Annotated[bool, typer.Option("--strict", help="Fail on omissions/low-confidence alterations")]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")]


def _build_config(
    config: Path | None = None,
    language: str | None = None,
    frameworks: list[str] | None = None,
    services: list[str] | None = None,
    provider: str | None = None,
    model: str | None = None,
    metadata_output: Path | None = None,
    openapi_output: Path | None = None,
    fmt: str | None = None,
    strict: bool = False,
    verbose: bool = False,
) -> AgentConfig:
    configure_logging(verbose=verbose)
    # The target repository path comes only from .env (PROJECT_ROOT) / config.yaml;
    # there is deliberately no --project-root CLI flag.
    overrides: dict[str, Any] = {
        "output": {
            "metadata_path": str(metadata_output) if metadata_output else None,
            "openapi_path": str(openapi_output) if openapi_output else None,
            "format": fmt,
        },
        "analysis": {
            "language": language,
            "frameworks": frameworks or None,
            "services": services or None,
        },
        "llm": {
            "provider": provider,
            "model": model,
        },
        "validation": {"strict": True} if strict else {},
    }
    cfg = load_config(config_file=config, cli_overrides=overrides)
    if cfg.llm.api_key is not None:
        register_secret(cfg.llm.api_key.get_secret_value())
    return cfg


def _require_llm(cfg: AgentConfig) -> None:
    """Fail fast unless a usable LLM provider is configured.

    OpenAPI generation strictly uses an LLM for descriptions; there is no
    ``--no-llm`` / template-only mode on the CLI. The provider and its API key
    are configured only via ``.env`` / ``config.yaml``.
    """
    provider = cfg.llm.provider
    if provider == "none":
        console.print(
            "[red]ERROR[/red] LLM is required. Set LLM_PROVIDER to "
            "gemini|anthropic|openai in .env (not 'none')."
        )
        raise typer.Exit(code=2)
    key = cfg.llm.api_key.get_secret_value() if cfg.llm.api_key else None
    if not key or key.startswith("your-"):
        console.print(
            f"[red]ERROR[/red] LLM is required but no usable {provider} API key was found. "
            f"Set the matching key in .env (e.g. GOOGLE_API_KEY for gemini)."
        )
        raise typer.Exit(code=2)


@app.command()
def analyze(
    config: ConfigOpt = None,
    language: LanguageOpt = None,
    framework: FrameworkOpt = None,
    service: ServiceOpt = None,
    metadata_output: MetadataOutOpt = None,
    strict: StrictOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Phase 1: statically analyze the repository (path from .env) and emit metadata JSON."""
    cfg = _build_config(
        config=config,
        language=language,
        frameworks=framework,
        services=service,
        metadata_output=metadata_output,
        strict=strict,
        verbose=verbose,
    )
    from openapi_agent.analysis.pipeline import run_analysis

    document = run_analysis(cfg, console=console)
    console.print(
        f"[green]Metadata written to[/green] {cfg.output.metadata_path} "
        f"({document.coverage.endpoints_total} endpoints, "
        f"{document.coverage.operations_total} operations)"
    )


@app.command()
def generate(
    config: ConfigOpt = None,
    provider: ProviderOpt = None,
    model: ModelOpt = None,
    metadata_output: MetadataOutOpt = None,
    openapi_output: OpenapiOutOpt = None,
    fmt: FormatOpt = None,
    strict: StrictOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Phase 2: generate OpenAPI 3.1 document(s) from existing metadata JSON (LLM required)."""
    cfg = _build_config(
        config=config,
        provider=provider,
        model=model,
        metadata_output=metadata_output,
        openapi_output=openapi_output,
        fmt=fmt,
        strict=strict,
        verbose=verbose,
    )
    _require_llm(cfg)
    from openapi_agent.openapi.generator import run_generation

    results = run_generation(cfg, console=console)
    for result in results:
        console.print(f"[green]OpenAPI written to[/green] {result.output_path}")


@app.command()
def run(
    config: ConfigOpt = None,
    language: LanguageOpt = None,
    framework: FrameworkOpt = None,
    service: ServiceOpt = None,
    provider: ProviderOpt = None,
    model: ModelOpt = None,
    metadata_output: MetadataOutOpt = None,
    openapi_output: OpenapiOutOpt = None,
    fmt: FormatOpt = None,
    strict: StrictOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Full pipeline: analyze + generate + validate + report (path from .env, LLM required)."""
    cfg = _build_config(
        config=config,
        language=language,
        frameworks=framework,
        services=service,
        provider=provider,
        model=model,
        metadata_output=metadata_output,
        openapi_output=openapi_output,
        fmt=fmt,
        strict=strict,
        verbose=verbose,
    )
    _require_llm(cfg)
    from openapi_agent.analysis.pipeline import run_analysis
    from openapi_agent.openapi.generator import run_generation
    from openapi_agent.reporting.report import render_report_table, write_report

    run_analysis(cfg, console=console)
    results = run_generation(cfg, console=console)
    report = write_report(cfg, results)
    render_report_table(report, console=console)
    for result in results:
        console.print(f"[green]OpenAPI written to[/green] {result.output_path}")
    console.print(f"[green]Report written to[/green] {cfg.output.report_path}")
    if cfg.validation.strict and not report.strict_ok:
        raise typer.Exit(code=2)
    if cfg.quality.fail_on_not_production and not report.production_ready:
        console.print("[red]ERROR[/red] output is not production-ready (see blocking issues above).")
        raise typer.Exit(code=3)


@app.command()
def validate(
    document: Annotated[Optional[Path], typer.Argument(help="OpenAPI document (defaults to configured output)")] = None,
    config: ConfigOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Validate an existing OpenAPI document (syntax, structure, refs, gates)."""
    cfg = _build_config(config=config, verbose=verbose)
    from openapi_agent.openapi.validators import validate_document_file

    target = document or cfg.output.openapi_path
    outcome = validate_document_file(target, cfg)
    for message in outcome.messages:
        style = {"error": "red", "warning": "yellow"}.get(message.severity, "dim")
        console.print(f"[{style}]{message.severity}[/{style}] {message.text}")
    if outcome.ok:
        console.print(f"[green]VALID[/green] {target}")
    else:
        console.print(f"[red]INVALID[/red] {target}")
        raise typer.Exit(code=1)


@app.command()
def report(
    config: ConfigOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Render the readiness/coverage report for the last run."""
    cfg = _build_config(config=config, verbose=verbose)
    from openapi_agent.reporting.report import load_report, render_report_table

    rpt = load_report(cfg.output.report_path)
    render_report_table(rpt, console=console)


@app.command()
def serve(
    config: ConfigOpt = None,
    openapi_output: OpenapiOutOpt = None,
    port: Annotated[Optional[int], typer.Option("--port", help="HTTP port (default 8081)")] = None,
    server: Annotated[Optional[str], typer.Option("--server", help="Bind address (default 127.0.0.1)")] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Serve the generated OpenAPI document in a local Swagger UI."""
    cfg = _build_config(config=config, openapi_output=openapi_output, verbose=verbose)
    from openapi_agent.serve.server import serve_docs

    serve_docs(
        spec_path=cfg.output.openapi_path,
        port=port or cfg.serve.port,
        host=server or "127.0.0.1",
        console=console,
    )


if __name__ == "__main__":
    app()
