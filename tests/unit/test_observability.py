"""G12 self-observability: /metrics, request ids, tracing, health, fallback."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from tenacity import wait_none

from src.api.app import app
from src.config.settings import Settings
from src.core.clustering.clusterer import ClusterData
from src.core.explain.confidence import compute_confidence
from src.core.explain.evidence import EvidencePacket
from src.core.explain.summarizer import explain_window
from src.core.explain.templates import render_text_summary
from src.core.llm.resilience import reset_llm_breaker
from src.observability.logging import configure_logging
from src.observability.metrics import (
    LLM_FALLBACK,
    REGISTRY,
    classify_http_path,
    queue_gauge_value,
    record_llm_fallback,
    reset_queue_gauge,
)
from src.observability.middleware import _apply_trace_headers, _w3c_trace_id_from_request_id

client = TestClient(app, raise_server_exceptions=False)

WINDOW_START = datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 3, 12, 14, 0, tzinfo=timezone.utc)


def _metrics_body() -> str:
    with patch("src.db.session.check_connection", return_value=False):
        resp = client.get("/metrics")
    assert resp.status_code == 200
    return resp.text


def _cluster() -> ClusterData:
    return ClusterData(
        fingerprint="abcd1234",
        representative_message="payment gateway 502",
        count=50,
        services={"checkout": 50},
        levels={"error": 50},
        first_seen=WINDOW_START,
        last_seen=WINDOW_END,
        baseline_count=0,
        change_ratio=51.0,
        importance_score=8.0,
    )


def _packet() -> EvidencePacket:
    return EvidencePacket(
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        total_logs=404,
        primary_cluster=_cluster(),
        secondary_clusters=[],
        trigger_candidates=[],
        evidence_items=["184 similar failures: payment gateway 502"],
        services_affected=["checkout"],
    )


class _BoomProvider:
    def generate_summary(self, evidence_packet: dict) -> str:
        raise httpx.TimeoutException("deadline")


def test_metrics_returns_prometheus_text() -> None:
    with patch("src.db.session.check_connection", return_value=False):
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    for name in (
        "raglogs_ingest_lines_total",
        "raglogs_ingest_duration_seconds",
        "raglogs_query_request_duration_seconds",
        "raglogs_llm_request_duration_seconds",
        "raglogs_llm_fallback_total",
        "raglogs_llm_breaker_state",
        "raglogs_cluster_count",
        "raglogs_purge_rows_total",
    ):
        assert name in body, f"missing metric {name}"


def test_metrics_exempt_from_auth() -> None:
    settings = Settings(auth_enabled=True, auth_mode="api_key", _env_file=None)
    with patch("src.config.get_settings", return_value=settings), \
         patch("src.db.session.check_connection", return_value=False):
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "raglogs_llm_fallback_total" in resp.text
    assert resp.headers.get("authorization") is None


def test_request_id_echoed_and_generated() -> None:
    with patch("src.db.session.check_connection", return_value=False):
        echoed = client.get("/health", headers={"X-Request-Id": "req-from-caller"})
        generated = client.get("/health")
    assert echoed.status_code == 200
    assert echoed.headers.get("X-Request-Id") == "req-from-caller"
    assert generated.headers.get("X-Request-Id")
    assert generated.headers.get("X-Request-Id") != "req-from-caller"


def test_request_id_present_on_401() -> None:
    settings = Settings(auth_enabled=True, auth_mode="api_key", _env_file=None)
    with patch("src.config.get_settings", return_value=settings):
        resp = client.post(
            "/query/explain",
            json={"since": "1h"},
            headers={"X-Request-ID": "req-unauth"},
        )
    assert resp.status_code == 401
    assert resp.headers.get("X-Request-Id") == "req-unauth"


def test_structlog_context_includes_request_id() -> None:
    captured: list[dict[str, Any]] = []

    def _capture(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        captured.append(dict(event_dict))
        return event_dict

    configure_logging(log_format="json", extra_processors=[_capture])
    try:
        with patch("src.db.session.check_connection", return_value=False):
            client.get("/health", headers={"X-Request-Id": "ctx-req-9"})
        http_events = [e for e in captured if e.get("event") == "http_request"]
        assert http_events, f"no http_request log; got {[e.get('event') for e in captured]}"
        assert any(e.get("request_id") == "ctx-req-9" for e in http_events)
    finally:
        configure_logging(log_format="json")


def test_trace_headers_on_query_response() -> None:
    resp = client.post("/v1/query/explain", json={"since": "1h"})
    assert "X-Trace-Id" in resp.headers
    traceparent = resp.headers.get("traceparent")
    assert traceparent is not None
    parts = traceparent.split("-")
    assert len(parts) == 4
    assert parts[0] == "00"
    assert len(parts[1]) == 32
    assert resp.headers["X-Trace-Id"] == parts[1]


def test_health_includes_llm_and_breaker_still_ok() -> None:
    reset_llm_breaker()
    with patch("src.db.session.check_connection", return_value=True), \
         patch("src.db.session.get_db") as get_db:
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.execute.return_value.scalar_one.return_value = 0
        get_db.side_effect = lambda: mock_db
        resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["llm_breaker"]["state"] == "closed"
    assert data["llm"]["provider"] == "disabled"
    assert data["llm"]["status"] == "disabled"


def test_g10_fallback_increments_counter() -> None:
    packet = _packet()
    settings = Settings(
        llm_provider="openai",
        openai_api_key="sk-test",
        _env_file=None,
    )
    before = LLM_FALLBACK._value.get()
    with patch(
        "src.core.explain.summarizer.run_clustering",
        return_value=(None, [packet.primary_cluster]),
    ), patch(
        "src.core.explain.summarizer.assemble_evidence",
        return_value=packet,
    ), patch(
        "src.core.explain.summarizer.get_settings",
        return_value=settings,
    ), patch(
        "src.core.explain.summarizer.build_llm_provider",
        return_value=_BoomProvider(),
    ), patch(
        "src.core.llm.resilience.default_llm_wait",
        return_value=wait_none(),
    ):
        result = explain_window(
            db=MagicMock(),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    assert result.mode == "rules"
    assert result.summary_text == render_text_summary(packet, compute_confidence(packet))
    assert LLM_FALLBACK._value.get() == before + 1
    body = _metrics_body()
    assert "raglogs_llm_fallback_total" in body


def test_classify_http_path() -> None:
    assert classify_http_path("POST", "/v1/query/explain") == ("query", "explain")
    assert classify_http_path("POST", "/query/ask") == ("query", "ask")
    assert classify_http_path("POST", "/v1/ingestions") == ("ingest", None)
    assert classify_http_path("POST", "/v1/ingestions/lines") == ("ingest", None)
    assert classify_http_path("POST", "/v1/ingestions/abc:pause") == ("ingest", None)
    assert classify_http_path("GET", "/v1/ingestions") == ("query", "ingestions")
    assert classify_http_path("GET", "/v1/ingestions/latest") == ("query", "ingestions")
    assert classify_http_path("GET", "/health") == ("other", None)
    assert classify_http_path("GET", "/metrics") == ("other", None)


def _histogram_count(name: str) -> float:
    for metric in REGISTRY.collect():
        if metric.name != name:
            continue
        for sample in metric.samples:
            if sample.name == f"{name}_count":
                return float(sample.value)
    return 0.0


def test_get_ingestions_not_counted_as_ingest_write() -> None:
    before = _histogram_count("raglogs_ingest_request_duration_seconds")
    with patch("src.db.session.get_db") as get_db:
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.execute.return_value.scalars.return_value.all.return_value = []
        mock_db.execute.return_value.scalar_one.return_value = 0
        get_db.side_effect = lambda: mock_db
        client.get("/v1/ingestions")
    assert _histogram_count("raglogs_ingest_request_duration_seconds") == before
    client.post("/v1/ingestions", json={"paths": ["/tmp/no-such-file.log"]})
    assert _histogram_count("raglogs_ingest_request_duration_seconds") == before + 1


def test_invalid_request_id_does_not_emit_illegal_traceparent() -> None:
    import re

    from starlette.responses import Response

    raw = "req-from-caller"
    hashed = _w3c_trace_id_from_request_id(raw)
    assert re.fullmatch(r"[0-9a-f]{32}", hashed)
    assert hashed != raw.replace("-", "")[:32].ljust(32, "0")

    with patch(
        "src.observability.middleware.current_trace_ids",
        return_value=(None, None, False),
    ):
        resp = Response()
        _apply_trace_headers(resp, raw)
    traceparent = resp.headers.get("traceparent")
    assert traceparent is not None
    parts = traceparent.split("-")
    assert len(parts) == 4
    assert parts[0] == "00"
    assert re.fullmatch(r"[0-9a-f]{32}", parts[1])
    assert "req" not in parts[1]
    assert resp.headers["X-Trace-Id"] == hashed

    with patch("src.db.session.check_connection", return_value=False):
        http = client.get("/health", headers={"X-Request-Id": raw})
    tp = http.headers.get("traceparent")
    assert tp is not None
    tid = tp.split("-")[1]
    assert re.fullmatch(r"[0-9a-f]{32}", tid)


def test_queue_depth_not_zero_on_db_failure() -> None:
    from src.observability.metrics import _set_queue_depth

    reset_queue_gauge()
    with patch("src.db.session.check_connection", return_value=False):
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert queue_gauge_value() is None
    assert "raglogs_worker_queue_depth" not in resp.text

    _set_queue_depth(7)
    assert queue_gauge_value() == 7.0
    with patch("src.db.session.check_connection", return_value=False):
        again = client.get("/metrics")
    assert queue_gauge_value() == 7.0
    assert "raglogs_worker_queue_depth 7" in again.text


def test_record_llm_fallback_shows_in_scrape() -> None:
    record_llm_fallback()
    names = {sample.name for metric in REGISTRY.collect() for sample in metric.samples}
    assert "raglogs_llm_fallback_total" in names
