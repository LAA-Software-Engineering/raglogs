from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/raglogs"

    # Connection pool. Sync route handlers run on FastAPI's threadpool (40
    # threads by default), so the pool must be sized in the same ballpark or
    # requests block on checkout and raise TimeoutError under load. Defaults
    # give 40 connections (20 + 20) to match that threadpool.
    db_pool_size: int = 20
    db_max_overflow: int = 20

    embeddings_provider: Literal["disabled", "openai", "local"] = "disabled"
    embeddings_model: str = "text-embedding-3-small"
    embeddings_dimensions: int = 1536

    # Semantic cluster merge (analysis-time; skipped when embeddings_provider=disabled)
    cluster_merge_similarity_threshold: float = 0.92
    cluster_merge_min_count: int = 1

    # Semantic ask (pgvector over stored log_embeddings; keyword fallback otherwise)
    ask_semantic_top_k: int = 100
    ask_semantic_min_similarity: float = 0.75

    # Similar-incident search (pgvector over cluster_embeddings; fingerprint fallback)
    similar_semantic_min_similarity: float = 0.80

    llm_provider: Literal["disabled", "openai", "ollama", "claude"] = "disabled"
    llm_model: str = "gpt-4.1-mini"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    ollama_base_url: str = "http://localhost:11434"
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"

    default_baseline_window: str = "24h"
    max_evidence_items: int = 8
    max_clusters_for_explain: int = 10

    # How far before window_start to search for trigger candidates (#76).
    trigger_lookback_minutes: int = 10

    # HTTP API authentication (G2). Default off so local demo and existing
    # TestClient tests stay unauthenticated. Production/Docker should set
    # AUTH_ENABLED=true. Keys are pinned to a scope (G8) unless minted with
    # allow_scope_override; every service read/write is filtered by scope.
    auth_enabled: bool = False
    auth_mode: Literal["api_key", "oidc", "both"] = "api_key"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    api_bind_host: str = "127.0.0.1"
    auth_refuse_insecure_bind: bool = False

    # Worker
    worker_poll_interval: int = 2  # seconds between idle polls

    # Data retention (G13). Duration strings like 30d / 180d. 0, empty, or
    # "off" disables that tier. Per-scope overrides live in scope_retention.
    retention_raw: str = "30d"
    retention_summary: str = "180d"
    purge_interval_seconds: int = 3600
    purge_chunk_size: int = 1000

    # Ingest backpressure (G9) and push/tail (G4). Tail ticks skip when the
    # pending worker queue is at ingest_queue_max.
    ingest_queue_max: int = 100
    ingest_retry_after_seconds: int = 5
    ingest_push_max_lines: int = 5000
    tail_poll_interval: int = 30
    tail_error_threshold: int = 5

    # API token-bucket rate limiting (G9). In-memory per process — not shared
    # across workers. Defaults are high so local demo and TestClient suites
    # are not 429'd. 0 rps = unlimited for that category. Identity is
    # auth_principal.key_id, else "anonymous" when AUTH_ENABLED=false.
    ratelimit_enabled: bool = True
    ratelimit_ingest_rps: float = 100.0
    ratelimit_query_rps: float = 100.0
    ratelimit_burst: float = 100.0
    ratelimit_retry_after_seconds: int = 1

    # Process-wide cap on in-flight LLM provider calls (G9). Independent of
    # API concurrency. 0 = unlimited. Noop provider skips the wait.
    llm_max_concurrency: int = 4

    # LLM resilience (G10). Timeouts/retries wrap every provider HTTP call.
    # On failure the pipeline falls back to deterministic templates.
    # 0 input-token budget means "derive from LLM_MAX_TOKENS".
    llm_timeout: float = 30.0
    llm_max_retries: int = 2
    llm_max_tokens: int = 600
    llm_max_input_tokens: int = 0
    llm_breaker_threshold: int = 5
    llm_breaker_cooldown_seconds: float = 60.0

    # HMAC-signed ingest completion webhooks (G5). WEBHOOK_SECRET is the
    # fallback when AUTH_ENABLED=false or the API key has no per-key secret.
    webhook_secret: str = ""
    webhook_max_retries: int = 5
    webhook_timeout: float = 10.0

    # Idempotency-Key TTL for POST /v1/ingestions (batch enqueue and tail create).
    ingest_idempotency_ttl_seconds: int = 86400

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

    # Self-observability (G12). LOG_FORMAT=json|console. OTEL_SDK_DISABLED
    # skips the SDK (tests/demo still get a request id). OTLP is optional and
    # off unless OTEL_EXPORTER_OTLP_ENDPOINT is set — no collector required.
    log_format: Literal["json", "console"] = "json"
    otel_sdk_disabled: bool = False
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "raglogs"

    # Cluster scoring weights
    severity_weight_fatal: float = 5.0
    severity_weight_error: float = 4.0
    severity_weight_warn: float = 3.0
    severity_weight_info: float = 1.0
    severity_weight_debug: float = 0.5

    # Confidence scoring (#83). Every threshold/weight the confidence scorer
    # uses is exposed here so it can be tuned against a labeled corpus rather
    # than edited in code. Defaults reproduce the original hand-picked values
    # exactly — changing them is the supported way to recalibrate. NOTE: these
    # have NOT been fitted to any corpus; on RCAEval the labels are in fact
    # anti-calibrated, but that is a log-only-cascade artifact, so the defaults
    # are left untouched pending a fit that generalizes (see the issue).
    confidence_count_high: int = 50  # primary cluster size for +2
    confidence_count_low: int = 10  # primary cluster size for +1
    confidence_change_ratio_high: float = 10.0  # change_ratio for +2
    confidence_change_ratio_low: float = 3.0  # change_ratio for +1
    confidence_points_count_high: int = 2
    confidence_points_count_low: int = 1
    confidence_points_change_high: int = 2
    confidence_points_change_low: int = 1
    confidence_points_trigger: int = 2
    confidence_points_secondary: int = 1
    confidence_points_multiservice: int = 1
    confidence_threshold_high: int = 5  # score for "high" (also requires trigger)
    confidence_threshold_medium_high: int = 4  # score for "medium-high"
    confidence_threshold_medium: int = 2  # score for "medium"
    # Ordinal rank (NOT a calibrated probability) each label maps to in the v1
    # schema's ``score`` field. See the field's schema description.
    confidence_score_low: float = 0.25
    confidence_score_medium: float = 0.50
    confidence_score_medium_high: float = 0.72
    confidence_score_high: float = 0.90

    # Root-cause candidate ranking (#82). The primary (service, fingerprint) is
    # scored by a weighted sum of log(error-volume), log(change_ratio+1) [anomaly
    # vs the in-job baseline], and onset earliness in the window [0..1].
    #
    # Anomaly and onset default to 0 => pure most-frequent-error volume, i.e. the
    # trivial baseline (28.9% RE3 / 8.0% RE2). Enabling them was measured on
    # RCAEval and did NOT robustly lift across corpora: at anomaly=1, onset=3 the
    # top-1 RCA was RE3 30.0% (+1.1pp, +1 case) but RE2 7.0% (-1.0pp, -1 case) —
    # a wash within single-case noise, because onset helps code faults (cause
    # precedes cascade) but hurts resource/network faults (no causal onset). The
    # mechanism is kept configurable for a corpus where it does generalize.
    rca_weight_volume: float = 1.0
    rca_weight_anomaly: float = 0.0
    rca_weight_onset: float = 0.0

    # Path to the learned multi-modal RCA ranker artifact (non-pickle JSON tree
    # ensemble, #118 C2). Empty = no model: the pipeline falls back to the
    # volume-based selector, so raglogs runs unchanged without an artifact.
    rca_ranker_model_path: str = ""


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
