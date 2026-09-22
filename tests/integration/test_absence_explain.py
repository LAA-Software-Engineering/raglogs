"""Integration test: Phase F (#184) absence-derived candidates end-to-end through explain_window
(product output) AND eval normalization. A service present in the baseline that goes silent in the
incident — while still emitting metrics (available) — becomes a real product candidate, and the eval
prediction derives it from that same field. Skipped without Postgres."""
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
SCOPE = "eval:absence-test"


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


def _span(trace, span_id, parent, service, ts):
    from src.core.ingestion.telemetry import ParsedSpan
    return ParsedSpan(trace_id=trace, span_id=span_id, parent_span_id=parent, service=service,
                      operation=f"{service} handle", start_time=ts, duration_ms=5.0, status_code="0")


def _seed(db):
    from src.core.ingestion.telemetry import (
        ParsedMetricSample,
        persist_metric_samples,
        persist_spans,
    )
    from src.db.models import LogEntry

    spans = []
    # checkout calls payment. In the BASELINE both emit spans; in the INCIDENT payment goes silent
    # (unreachable) while checkout stays active and errors.
    for i in range(60):
        t = BASELINE_START + timedelta(seconds=i * 5)
        spans.append(_span(f"b{i}", f"c{i}", None, "checkout", t))
        spans.append(_span(f"b{i}", f"p{i}", f"c{i}", "payment", t + timedelta(milliseconds=2)))
    for i in range(60):  # incident: only checkout emits (payment vanished)
        spans.append(_span(f"i{i}", f"ic{i}", None, "checkout", INJECT + timedelta(seconds=i * 5)))
    persist_spans(db, spans, scope=SCOPE)

    # checkout error logs (the loud symptom -> a primary cluster / top-1)
    for i in range(20):
        db.add(LogEntry(
            timestamp=INJECT + timedelta(seconds=i), service="checkout", level="error",
            raw_message="payment unreachable", normalized_message="payment unreachable",
            fingerprint="co", scope=SCOPE,
        ))
    # payment still emits metrics in the incident -> independently available (up, but unreachable)
    persist_metric_samples(db, [
        ParsedMetricSample(service="payment", metric="error_rate", value=0.01,
                           ts=BASELINE_START + timedelta(seconds=5)),
        ParsedMetricSample(service="payment", metric="error_rate", value=0.01,
                           ts=INJECT + timedelta(seconds=30)),
    ], scope=SCOPE)
    db.flush()


def test_absence_candidate_is_a_product_candidate_and_eval_prediction(db_session):
    from src.core.explain.summarizer import explain_window
    from src.eval.runner import prediction_from_result

    _seed(db_session)
    result = explain_window(
        db=db_session, window_start=INJECT, window_end=WINDOW_END,
        no_llm=True, baseline_window_str="300s", scope=SCOPE,
    )

    # PRODUCT: the vanished service is in its own product field (a distinct signal, NOT mixed into the
    # scored ranker candidates) that the API/CLI serialize.
    assert "payment" in result.absence_candidates
    assert all(c.get("service") != "payment" for c in result.root_cause_candidates)  # not a ranker entry

    # API serialization exposes it truthfully, separate from the scored candidates.
    from src.api.schemas.v1 import explain_from_result
    resp = explain_from_result(result, no_llm=True, cached=False, scope=SCOPE)
    assert "payment" in resp.absence_candidates

    # EVAL: the prediction is derived from that same product field — not fabricated in the adapter.
    pred = prediction_from_result(result)
    assert "payment" in pred.predicted_services
    assert pred.root_cause_service != "payment"  # unranked: the loud symptom stays top-1 (Phase G ranks)
