from pathlib import Path

from openapi_agent.config.loader import load_config
from openapi_agent.config.settings import EnvSettings


def test_defaults_without_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no config.yaml, no .env
    config = load_config(env=EnvSettings(_env_file=None))
    assert config.llm.provider == "gemini"
    assert config.output.format == "yaml"
    assert config.validation.strict is False


def test_precedence_cli_over_yaml_over_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "project_root: ./from-yaml\nllm:\n  provider: openai\n", encoding="utf-8"
    )
    env = EnvSettings(_env_file=None, PROJECT_ROOT="./from-env", LLM_PROVIDER="anthropic")
    config = load_config(
        config_file=tmp_path / "config.yaml",
        cli_overrides={"llm": {"provider": "gemini"}},
        env=env,
    )
    assert config.project_root == Path("./from-yaml")  # yaml beats env
    assert config.llm.provider == "gemini"  # CLI beats yaml


def test_none_cli_values_do_not_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = EnvSettings(_env_file=None, OUTPUT_FORMAT="json")
    config = load_config(cli_overrides={"output": {"format": None}}, env=env)
    assert config.output.format == "json"


def test_exclude_dirs_merge_defaults():
    from openapi_agent.config.loader import AnalysisConfig

    analysis = AnalysisConfig(exclude_dirs=["docs"])
    excluded = analysis.effective_exclude_dirs()
    assert {"docs", ".venv", "node_modules", "target", "build", "migrations"} <= excluded
    assert "migrations" not in AnalysisConfig(include_migrations=True).effective_exclude_dirs()


def test_api_key_matches_provider(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = EnvSettings(
        _env_file=None, LLM_PROVIDER="openai", OPENAI_API_KEY="sk-test-abcdef123456"
    )
    config = load_config(env=env)
    assert config.llm.api_key is not None
    assert config.llm.api_key.get_secret_value() == "sk-test-abcdef123456"
    assert config.llm.model == "gpt-4o-mini"
