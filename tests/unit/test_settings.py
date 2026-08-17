"""Tests for unprefixed environment variable names on Settings."""
from src.config.settings import Settings


def test_reads_unprefixed_env(monkeypatch):
    monkeypatch.setenv("DB_URL", "postgresql+psycopg://unit:unit@localhost/unit")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ADAPTER_CLOUDWATCH_REGION", "eu-west-1")
    monkeypatch.setenv("LOKI_URL", "http://loki:3100")

    settings = Settings(_env_file=None)

    assert settings.db_url == "postgresql+psycopg://unit:unit@localhost/unit"
    assert settings.llm_provider == "openai"
    assert settings.openai_api_key == "sk-test"
    assert settings.adapter_cloudwatch_region == "eu-west-1"
    assert settings.loki_url == "http://loki:3100"


def test_cluster_merge_settings_from_env(monkeypatch):
    monkeypatch.setenv("CLUSTER_MERGE_SIMILARITY_THRESHOLD", "0.95")
    monkeypatch.setenv("CLUSTER_MERGE_MIN_COUNT", "2")
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openai")

    settings = Settings(_env_file=None)

    assert settings.cluster_merge_similarity_threshold == 0.95
    assert settings.cluster_merge_min_count == 2
    assert settings.embeddings_provider == "openai"


def test_ignores_legacy_raglogs_prefix(monkeypatch):
    monkeypatch.setenv("RAGLOGS_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("RAGLOGS_DB_URL", "postgresql+psycopg://legacy/db")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DB_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.llm_provider == "disabled"
    assert settings.db_url == "postgresql+psycopg://postgres:postgres@localhost:5432/raglogs"
