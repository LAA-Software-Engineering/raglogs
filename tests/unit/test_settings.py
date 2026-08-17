"""Tests for unprefixed environment variable names on Settings."""
import pytest

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


def test_ask_semantic_settings_from_env(monkeypatch):
    monkeypatch.setenv("ASK_SEMANTIC_TOP_K", "50")
    monkeypatch.setenv("ASK_SEMANTIC_MIN_SIMILARITY", "0.6")

    settings = Settings(_env_file=None)

    assert settings.ask_semantic_top_k == 50
    assert settings.ask_semantic_min_similarity == pytest.approx(0.6)


def test_ignores_legacy_raglogs_prefix(monkeypatch):
    monkeypatch.setenv("RAGLOGS_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("RAGLOGS_DB_URL", "postgresql+psycopg://legacy/db")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DB_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.llm_provider == "disabled"
    assert settings.db_url == "postgresql+psycopg://postgres:postgres@localhost:5432/raglogs"


def test_auth_settings_defaults_disabled():
    settings = Settings(_env_file=None)
    assert settings.auth_enabled is False
    assert settings.auth_mode == "api_key"
    assert settings.oidc_issuer == ""
    assert settings.api_bind_host == "127.0.0.1"
    assert settings.auth_refuse_insecure_bind is False


def test_auth_settings_from_env(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "both")
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("OIDC_AUDIENCE", "raglogs")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://idp.example/jwks.json")
    monkeypatch.setenv("API_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("AUTH_REFUSE_INSECURE_BIND", "true")

    settings = Settings(_env_file=None)

    assert settings.auth_enabled is True
    assert settings.auth_mode == "both"
    assert settings.oidc_issuer == "https://idp.example"
    assert settings.oidc_audience == "raglogs"
    assert settings.oidc_jwks_url == "https://idp.example/jwks.json"
    assert settings.api_bind_host == "0.0.0.0"
    assert settings.auth_refuse_insecure_bind is True


def test_ingest_backpressure_and_tail_settings_from_env(monkeypatch):
    monkeypatch.setenv("INGEST_QUEUE_MAX", "10")
    monkeypatch.setenv("INGEST_RETRY_AFTER_SECONDS", "8")
    monkeypatch.setenv("INGEST_PUSH_MAX_LINES", "100")
    monkeypatch.setenv("TAIL_POLL_INTERVAL", "15")
    monkeypatch.setenv("TAIL_ERROR_THRESHOLD", "3")

    settings = Settings(_env_file=None)

    assert settings.ingest_queue_max == 10
    assert settings.ingest_retry_after_seconds == 8
    assert settings.ingest_push_max_lines == 100
    assert settings.tail_poll_interval == 15
    assert settings.tail_error_threshold == 3
