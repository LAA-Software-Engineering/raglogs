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


def test_similar_semantic_settings_from_env(monkeypatch):
    monkeypatch.setenv("SIMILAR_SEMANTIC_MIN_SIMILARITY", "0.85")

    settings = Settings(_env_file=None)

    assert settings.similar_semantic_min_similarity == pytest.approx(0.85)


def test_ignores_legacy_raglogs_prefix(monkeypatch):
    monkeypatch.setenv("RAGLOGS_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("RAGLOGS_DB_URL", "postgresql+psycopg://legacy/db")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DB_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.llm_provider == "disabled"
    assert (
        settings.db_url
        == "postgresql+psycopg://postgres:postgres@localhost:5432/raglogs"
    )


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


def test_webhook_settings_defaults():
    settings = Settings(_env_file=None)
    assert settings.webhook_secret == ""
    assert settings.webhook_max_retries == 5
    assert settings.webhook_timeout == 10.0


def test_webhook_settings_from_env(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "whsec_global")
    monkeypatch.setenv("WEBHOOK_MAX_RETRIES", "3")
    monkeypatch.setenv("WEBHOOK_TIMEOUT", "7.5")

    settings = Settings(_env_file=None)

    assert settings.webhook_secret == "whsec_global"
    assert settings.webhook_max_retries == 3
    assert settings.webhook_timeout == pytest.approx(7.5)


def test_ingest_idempotency_ttl_default():
    settings = Settings(_env_file=None)
    assert settings.ingest_idempotency_ttl_seconds == 86400


def test_ingest_idempotency_ttl_from_env(monkeypatch):
    monkeypatch.setenv("INGEST_IDEMPOTENCY_TTL_SECONDS", "60")
    settings = Settings(_env_file=None)
    assert settings.ingest_idempotency_ttl_seconds == 60


def test_ratelimit_and_llm_concurrency_defaults():
    settings = Settings(_env_file=None)
    assert settings.ratelimit_enabled is True
    assert settings.ratelimit_ingest_rps == 100.0
    assert settings.ratelimit_query_rps == 100.0
    assert settings.ratelimit_burst == 100.0
    assert settings.ratelimit_retry_after_seconds == 1
    assert settings.llm_max_concurrency == 4
    assert settings.ingest_queue_max == 100
    assert settings.llm_timeout == 30.0
    assert settings.llm_max_retries == 2
    assert settings.llm_max_tokens == 600
    assert settings.llm_max_input_tokens == 0
    assert settings.llm_breaker_threshold == 5
    assert settings.llm_breaker_cooldown_seconds == 60.0


def test_ratelimit_and_llm_concurrency_from_env(monkeypatch):
    monkeypatch.setenv("RATELIMIT_ENABLED", "false")
    monkeypatch.setenv("RATELIMIT_INGEST_RPS", "2")
    monkeypatch.setenv("RATELIMIT_QUERY_RPS", "3")
    monkeypatch.setenv("RATELIMIT_BURST", "5")
    monkeypatch.setenv("RATELIMIT_RETRY_AFTER_SECONDS", "9")
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("INGEST_QUEUE_MAX", "7")

    settings = Settings(_env_file=None)

    assert settings.ratelimit_enabled is False
    assert settings.ratelimit_ingest_rps == 2.0
    assert settings.ratelimit_query_rps == 3.0
    assert settings.ratelimit_burst == 5.0
    assert settings.ratelimit_retry_after_seconds == 9
    assert settings.llm_max_concurrency == 1
    assert settings.ingest_queue_max == 7


def test_llm_resilience_settings_from_env(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT", "15")
    monkeypatch.setenv("LLM_MAX_RETRIES", "4")
    monkeypatch.setenv("LLM_MAX_TOKENS", "200")
    monkeypatch.setenv("LLM_MAX_INPUT_TOKENS", "3000")
    monkeypatch.setenv("LLM_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("LLM_BREAKER_COOLDOWN_SECONDS", "45")

    settings = Settings(_env_file=None)

    assert settings.llm_timeout == pytest.approx(15.0)
    assert settings.llm_max_retries == 4
    assert settings.llm_max_tokens == 200
    assert settings.llm_max_input_tokens == 3000
    assert settings.llm_breaker_threshold == 3
    assert settings.llm_breaker_cooldown_seconds == pytest.approx(45.0)
