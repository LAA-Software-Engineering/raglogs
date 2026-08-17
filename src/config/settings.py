from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/raglogs"

    embeddings_provider: Literal["disabled", "openai", "local"] = "disabled"
    embeddings_model: str = "text-embedding-3-small"
    embeddings_dimensions: int = 1536

    # Semantic cluster merge (analysis-time; skipped when embeddings_provider=disabled)
    cluster_merge_similarity_threshold: float = 0.92
    cluster_merge_min_count: int = 1

    # Semantic ask (pgvector over stored log_embeddings; keyword fallback otherwise)
    ask_semantic_top_k: int = 100
    ask_semantic_min_similarity: float = 0.75

    llm_provider: Literal["disabled", "openai", "ollama"] = "disabled"
    llm_model: str = "gpt-4.1-mini"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    ollama_base_url: str = "http://localhost:11434"

    default_baseline_window: str = "24h"
    max_evidence_items: int = 8
    max_clusters_for_explain: int = 10

    # HTTP API authentication (G2). Default off so local demo and existing
    # TestClient tests stay unauthenticated. Production/Docker should set
    # AUTH_ENABLED=true. Scope on keys is stored for later G8 isolation;
    # queries are not filtered by scope yet.
    auth_enabled: bool = False
    auth_mode: Literal["api_key", "oidc", "both"] = "api_key"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    api_bind_host: str = "127.0.0.1"
    auth_refuse_insecure_bind: bool = False

    # Worker
    worker_poll_interval: int = 2          # seconds between idle polls

    # Ingest backpressure (minimal G9 stand-in) and push/tail (G4)
    ingest_queue_max: int = 100
    ingest_retry_after_seconds: int = 5
    ingest_push_max_lines: int = 5000
    tail_poll_interval: int = 30
    tail_error_threshold: int = 5

    # Adapters
    adapter_cloudwatch_region: str = "us-east-1"
    datadog_api_key: str = ""
    datadog_app_key: str = ""
    datadog_site: str = "datadoghq.com"
    datadog_page_size: int = 1000
    datadog_max_rows: int = 10000
    loki_url: str = ""
    loki_tenant: str = ""
    loki_bearer_token: str = ""
    loki_username: str = ""
    loki_password: str = ""
    loki_query: str = ""

    # Cluster scoring weights
    severity_weight_fatal: float = 5.0
    severity_weight_error: float = 4.0
    severity_weight_warn: float = 3.0
    severity_weight_info: float = 1.0
    severity_weight_debug: float = 0.5


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = Settings()
    return _settings
