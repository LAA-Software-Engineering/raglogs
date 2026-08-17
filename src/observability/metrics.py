"""Prometheus metrics for ingest, query, clustering, LLM, breaker, and queue."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Isolated registry so /metrics is only raglogs series (no default process
# collectors) and unit tests can scrape without a Prometheus server.
REGISTRY = CollectorRegistry()

_QUERY_ENDPOINTS: tuple[str, ...] = (
    "explain",
    "ask",
    "clusters",
    "timeline",
    "compare",
    "similar",
    "ingestions",
)
_INGEST_RESULTS: tuple[str, ...] = ("inserted", "deduped", "error")

INGEST_DURATION = Histogram(
    "raglogs_ingest_duration_seconds",
    "Wall time of an ingest pipeline run (files, adapter, or push).",
    registry=REGISTRY,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
INGEST_LINES = Counter(
    "raglogs_ingest_lines_total",
    "Log lines handled during ingest, by persistence result.",
    ["result"],
    registry=REGISTRY,
)
INGEST_REQUEST_DURATION = Histogram(
    "raglogs_ingest_request_duration_seconds",
    "HTTP latency of ingest write endpoints (POST /v1/ingestions*).",
    registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
CLUSTER_COUNT = Histogram(
    "raglogs_cluster_count",
    "Clusters produced by a clustering run (after cap/merge).",
    registry=REGISTRY,
    buckets=(0, 1, 2, 5, 10, 20, 50, 100, 200),
)
QUERY_REQUEST_DURATION = Histogram(
    "raglogs_query_request_duration_seconds",
    "HTTP latency of query endpoints, labeled by operation.",
    ["endpoint"],
    registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
LLM_REQUEST_DURATION = Histogram(
    "raglogs_llm_request_duration_seconds",
    "Latency of LLM provider invocations (breaker + retries included).",
    registry=REGISTRY,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
LLM_FALLBACK = Counter(
    "raglogs_llm_fallback_total",
    "Times explain/ask fell back to deterministic templates (G10).",
    registry=REGISTRY,
)
LLM_ESTIMATED_TOKENS = Counter(
    "raglogs_llm_estimated_tokens_total",
    "Estimated LLM input tokens (UTF-8 chars/4 heuristic, not USD cost).",
    registry=REGISTRY,
)
LLM_BREAKER_STATE = Gauge(
    "raglogs_llm_breaker_state",
    "LLM circuit breaker: 0=closed, 1=half_open, 2=open.",
    registry=REGISTRY,
)
_QUEUE_GAUGE_NAME = "raglogs_worker_queue_depth"
_QUEUE_GAUGE_HELP = (
    "Pending worker jobs. Published after a successful scrape and left stale "
    "if the database is unreachable (never set to 0 solely because DB is down)."
)
_queue_gauge: Gauge | None = None
_QUEUE_STATEMENT_TIMEOUT_MS = 2000

# Pre-register labeled series so /metrics always includes the names/labels
# even before the first observation (Prometheus scrapes empty time series).
for _result in _INGEST_RESULTS:
    INGEST_LINES.labels(result=_result)
for _endpoint in _QUERY_ENDPOINTS:
    QUERY_REQUEST_DURATION.labels(endpoint=_endpoint)

BREAKER_STATE_VALUES: dict[str, float] = {
    "closed": 0.0,
    "half_open": 1.0,
    "open": 2.0,
}


def record_ingest_duration(seconds: float) -> None:
    INGEST_DURATION.observe(max(0.0, seconds))


def record_ingest_lines(count: int, *, result: str) -> None:
    if count <= 0:
        return
    label = result if result in _INGEST_RESULTS else "inserted"
    INGEST_LINES.labels(result=label).inc(count)


def record_ingest_request_duration(seconds: float) -> None:
    INGEST_REQUEST_DURATION.observe(max(0.0, seconds))


def record_cluster_count(count: int) -> None:
    CLUSTER_COUNT.observe(max(0, count))


def record_query_request_duration(endpoint: str, seconds: float) -> None:
    label = endpoint if endpoint in _QUERY_ENDPOINTS else "explain"
    QUERY_REQUEST_DURATION.labels(endpoint=label).observe(max(0.0, seconds))


def record_llm_duration(seconds: float) -> None:
    LLM_REQUEST_DURATION.observe(max(0.0, seconds))


def record_llm_fallback() -> None:
    LLM_FALLBACK.inc()


def record_llm_estimated_tokens(count: int) -> None:
    if count > 0:
        LLM_ESTIMATED_TOKENS.inc(count)


def refresh_runtime_gauges() -> None:
    """Update breaker + queue gauges just before a /metrics scrape."""
    _refresh_breaker_gauge()
    _refresh_queue_depth()


def _refresh_breaker_gauge() -> None:
    from src.core.llm.resilience import breaker_health

    snap = breaker_health()
    state = str(snap.get("state", "closed"))
    LLM_BREAKER_STATE.set(BREAKER_STATE_VALUES.get(state, 0.0))


def _set_queue_depth(depth: int) -> None:
    """Publish a successful queue-depth scrape (registers the gauge on first set)."""
    global _queue_gauge
    if _queue_gauge is None:
        _queue_gauge = Gauge(_QUEUE_GAUGE_NAME, _QUEUE_GAUGE_HELP, registry=REGISTRY)
    _queue_gauge.set(int(depth))


def reset_queue_gauge() -> None:
    """Drop the queue gauge so tests start from 'never scraped'."""
    global _queue_gauge
    if _queue_gauge is not None:
        REGISTRY.unregister(_queue_gauge)
        _queue_gauge = None


def queue_gauge_value() -> float | None:
    """Current published queue depth, or None if never successfully scraped."""
    if _queue_gauge is None:
        return None
    for metric in REGISTRY.collect():
        if metric.name != _QUEUE_GAUGE_NAME:
            continue
        for sample in metric.samples:
            if sample.name == _QUEUE_GAUGE_NAME:
                return float(sample.value)
    return None


def _refresh_queue_depth() -> None:
    from src.db.session import check_connection, get_db

    if not check_connection():
        return
    try:
        from sqlalchemy import func, select, text

        from src.db.models import WorkerJob

        with get_db() as db:
            try:
                db.execute(text(f"SET LOCAL statement_timeout = '{_QUEUE_STATEMENT_TIMEOUT_MS}'"))
            except Exception:
                pass
            depth = db.execute(
                select(func.count()).select_from(WorkerJob).where(WorkerJob.status == "pending")
            ).scalar_one()
        _set_queue_depth(int(depth or 0))
    except Exception:
        return


def classify_http_path(method: str, path: str) -> tuple[str, str | None]:
    """Return ``(kind, query_endpoint)`` for HTTP latency recording.

    ``kind`` is ``ingest`` (POST writes only), ``query``, or ``other``.
    GET ingest list/status is ``query`` so it does not pollute ingest p99.
    """
    import re

    normalized = path or "/"
    if len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    normalized = re.sub(r"^/v\d+(?=/|$)", "", normalized) or "/"
    verb = method.upper()

    if normalized == "/ingestions" or normalized.startswith("/ingestions/"):
        if verb == "POST":
            return "ingest", None
        return "query", "ingestions"
    if normalized == "/query" or normalized.startswith("/query/"):
        rest = normalized[len("/query/") :] if normalized.startswith("/query/") else ""
        operation = rest.split("/")[0] if rest else "explain"
        if operation not in _QUERY_ENDPOINTS:
            operation = "explain"
        return "query", operation
    return "other", None
