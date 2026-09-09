"""Integration test for compute_features: the DB layer that reads persisted
logs/traces/metrics into the multi-modal RCA feature table (#118 C1b). Skipped
without Postgres."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)

INJECT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
INCIDENT_END = INJECT + timedelta(seconds=600)
BASELINE_START = INJECT - timedelta(seconds=300)
SCOPE = "eval:rca-features-test"


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


def test_compute_features_reads_all_three_modalities(db_session) -> None:
    from src.core.ingestion.telemetry import (
        ParsedMetricSample,
        ParsedSpan,
        persist_metric_samples,
        persist_spans,
    )
    from src.core.rca.features import compute_features
    from src.db.models import LogEntry

    # logs: an error cluster on "cart" in the incident window
    for i in range(3):
        db_session.add(
            LogEntry(
                timestamp=INJECT + timedelta(seconds=i + 1),
                service="cart",
                level="error",
                raw_message="at com.foo.Bar(Bar.java:12)",
                normalized_message="at com.foo.Bar(Bar.java:12)",
                fingerprint="fp-a",
                scope=SCOPE,
            )
        )
    # traces: baseline vs incident for "cart" (rate should rise)
    spans = [ParsedSpan(trace_id="t", span_id="b", service="cart", start_time=BASELINE_START + timedelta(seconds=5), duration_ms=100.0)]
    spans += [ParsedSpan(trace_id="t", span_id=f"i{i}", service="cart", start_time=INJECT + timedelta(seconds=i), duration_ms=400.0) for i in range(4)]
    persist_spans(db_session, spans, scope=SCOPE)
    # metrics: cpu jumps on "cart"
    persist_metric_samples(
        db_session,
        [
            ParsedMetricSample(service="cart", metric="cpu", value=1.0, ts=BASELINE_START + timedelta(seconds=5)),
            ParsedMetricSample(service="cart", metric="cpu", value=3.0, ts=INJECT + timedelta(seconds=5)),
        ],
        scope=SCOPE,
    )
    db_session.flush()

    table = compute_features(
        db_session, SCOPE, incident_start=INJECT, incident_end=INCIDENT_END, baseline_start=BASELINE_START
    )
    assert table.has_logs and table.has_traces and table.has_metrics
    by = {s.service: s for s in table.services}
    assert "cart" in by
    cart = by["cart"]
    assert cart.log_err == 3
    assert cart.log_grp == 3  # one (service, fingerprint) group of 3
    assert cart.log_stack == 3  # every line is a stack-trace line
    assert cart.tr_rate == pytest.approx(2.0, rel=1e-3)  # (4/600)/(1/300)
    assert cart.met_anom == pytest.approx(2.0, rel=1e-6)  # |3-1|/1
    assert cart.has_logs == 1 and cart.has_traces == 1 and cart.has_metrics == 1


def test_scope_isolation(db_session) -> None:
    from src.core.rca.features import compute_features
    from src.db.models import LogEntry

    db_session.add(
        LogEntry(
            timestamp=INJECT + timedelta(seconds=1), service="other", level="error",
            raw_message="boom", normalized_message="boom", fingerprint="x", scope="eval:someone-else",
        )
    )
    db_session.flush()
    table = compute_features(
        db_session, SCOPE, incident_start=INJECT, incident_end=INCIDENT_END, baseline_start=BASELINE_START
    )
    assert table.services == []  # nothing under this scope
