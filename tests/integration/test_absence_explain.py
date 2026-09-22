"""Integration test: Phase F (#184, evidence-only) silent-service detection end-to-end through
explain_window. A service present in the baseline that goes silent in the incident is surfaced as
observed *evidence* (`absence_candidates` + an evidence item, serialized by the API) but must NOT
become a causal candidate — it never enters `generated_candidates`, `root_cause_candidates`, or the
eval `predicted_services`. Skipped without Postgres."""
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


def _span(trace, span_id, parent, service, ts, status_code="0"):
    from src.core.ingestion.telemetry import ParsedSpan
    return ParsedSpan(trace_id=trace, span_id=span_id, parent_span_id=parent, service=service,
                      operation=f"{service} handle", start_time=ts, duration_ms=5.0,
                      status_code=status_code)


def _seed(db):
    from src.core.ingestion.telemetry import (
        ParsedMetricSample,
        persist_metric_samples,
        persist_spans,
    )
    from src.db.models import LogEntry

    spans = []
    # checkout calls payment. In the BASELINE both emit spans; in the INCIDENT payment goes silent
    # while checkout stays active. Silence alone -> evidence, not a causal candidate.
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
    # payment still emits metrics in the incident (it is "up" but unreachable). This is deliberately
    # NOT what the mechanism keys on — availability comes from the failed-attempt edge, not metrics —
    # and is kept only to show metric presence does not by itself create or suppress the candidate.
    persist_metric_samples(db, [
        ParsedMetricSample(service="payment", metric="error_rate", value=0.01,
                           ts=BASELINE_START + timedelta(seconds=5)),
        ParsedMetricSample(service="payment", metric="error_rate", value=0.01,
                           ts=INJECT + timedelta(seconds=30)),
    ], scope=SCOPE)
    db.flush()


def test_silent_service_is_evidence_only_not_a_causal_candidate(db_session):
    from src.core.explain.summarizer import explain_window
    from src.eval.runner import prediction_from_result

    _seed(db_session)
    result = explain_window(
        db=db_session, window_start=INJECT, window_end=WINDOW_END,
        no_llm=True, baseline_window_str="300s", scope=SCOPE,
    )

    # EVIDENCE: the vanished service is surfaced as observed evidence in its own field...
    assert "payment" in result.absence_candidates
    # ...but is NOT promoted into any causal-candidate surface.
    assert all(c.get("service") != "payment" for c in result.root_cause_candidates)
    assert "payment" not in (result.generated_candidates or [])

    # API serialization exposes the evidence, separate from the scored candidates.
    from src.api.schemas.v1 import explain_from_result
    resp = explain_from_result(result, no_llm=True, cached=False, scope=SCOPE)
    assert "payment" in resp.absence_candidates
    assert all(c.service != "payment" for c in resp.root_cause_candidates)

    # EVAL: silence does not affect the causal prediction / coverage.
    pred = prediction_from_result(result)
    assert "payment" not in pred.predicted_services
    assert "payment" not in (pred.generated_candidates or [])
