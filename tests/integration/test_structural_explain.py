"""Integration test: Phase I (#187) opt-in structural view end-to-end through explain_window.

Seeds real metric/trace telemetry where one service is anomalous, then checks that the experimental
structural view (a) is produced only when `structural=True`, (b) runs the validated A–E model over the
live DB rows to a real outcome, and (c) never alters the ordinary explanation. Skipped without Postgres.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)

INJECT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
WINDOW_END = INJECT + timedelta(seconds=600)
BASELINE_START = INJECT - timedelta(seconds=300)
SCOPE = "eval:structural-test"


@pytest.fixture
def db_session():
    from sqlalchemy import text

    from src.db.models import Base
    from src.db.session import check_connection, get_db, get_engine

    if not check_connection():
        pytest.skip("Cannot connect to database")
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with get_db() as db:
        yield db


def _seed(db):
    from src.core.ingestion.telemetry import (
        ParsedMetricSample,
        ParsedSpan,
        persist_metric_samples,
        persist_spans,
    )
    from src.db.models import LogEntry

    # checkout -> payment; payment's error-rate spikes in the incident (anomalous), checkout stays low.
    spans = []
    for i in range(30):
        t = INJECT + timedelta(seconds=i * 5)
        spans.append(ParsedSpan(trace_id=f"t{i}", span_id=f"c{i}", parent_span_id=None,
                                service="checkout", operation="checkout", start_time=t,
                                duration_ms=5.0, status_code="0"))
        spans.append(ParsedSpan(trace_id=f"t{i}", span_id=f"p{i}", parent_span_id=f"c{i}",
                                service="payment", operation="payment",
                                start_time=t + timedelta(milliseconds=2), duration_ms=5.0,
                                status_code="0"))
    persist_spans(db, spans, scope=SCOPE)

    metrics = []
    for i in range(6):
        base_t = BASELINE_START + timedelta(seconds=i * 10)
        inc_t = INJECT + timedelta(seconds=i * 10)
        metrics.append(ParsedMetricSample(service="payment", metric="error_rate", value=0.01, ts=base_t))
        metrics.append(ParsedMetricSample(service="payment", metric="error_rate", value=0.4, ts=inc_t))
        metrics.append(ParsedMetricSample(service="checkout", metric="error_rate", value=0.01, ts=inc_t))
    persist_metric_samples(db, metrics, scope=SCOPE)

    # a little log noise so the ordinary explanation has something to say
    for i in range(10):
        db.add(LogEntry(timestamp=INJECT + timedelta(seconds=i), service="payment", level="error",
                        raw_message="payment failing", normalized_message="payment failing",
                        fingerprint="pf", scope=SCOPE))
    db.flush()


def test_structural_view_is_opt_in_and_additive(db_session):
    from src.core.explain.summarizer import explain_window

    _seed(db_session)
    common = dict(db=db_session, window_start=INJECT, window_end=WINDOW_END, no_llm=True,
                  baseline_window_str="300s", scope=SCOPE)

    off = explain_window(**common)
    assert off.structural_result is None  # default: no structural view

    on = explain_window(**common, structural=True)
    assert on.structural_result is not None
    # The validated model localizes the anomalous service.
    assert "payment" in on.structural_result.localization

    # Additive: enabling the structural view does not change the ordinary explanation.
    assert on.summary_text == off.summary_text
    assert on.services_affected == off.services_affected
    assert on.primary_cluster == off.primary_cluster

    # API serialization exposes the packet only when present.
    from src.api.schemas.v1 import explain_from_result
    assert explain_from_result(off, no_llm=True, cached=False, scope=SCOPE).structural is None
    resp_on = explain_from_result(on, no_llm=True, cached=False, scope=SCOPE)
    assert resp_on.structural is not None
    assert "payment" in resp_on.structural["localization"]


def test_structural_adapter_failure_does_not_break_the_explanation(db_session, monkeypatch):
    # The isolation guarantee: an exception anywhere in the structural build must degrade to "no
    # structural view", never take down the already-computed ordinary explanation.
    import src.core.rca.structural as structural_mod
    from src.core.explain.summarizer import explain_window

    _seed(db_session)

    def _boom(*a, **k):
        raise RuntimeError("simulated structural-model bug")

    monkeypatch.setattr(structural_mod, "build_structural_view", _boom)

    result = explain_window(
        db=db_session, window_start=INJECT, window_end=WINDOW_END, no_llm=True,
        baseline_window_str="300s", scope=SCOPE, structural=True,
    )
    # Ordinary explanation survives; structural degrades to None.
    assert result.structural_result is None
    assert result.summary_text  # the ordinary result is intact, not lost to the adapter crash
    assert result.services_affected
