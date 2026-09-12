"""Integration test: the abstention gate wired into explain_window (#79).

Opt-in (default off). When enabled, a window whose logs+metrics show no
incident-strength anomaly vs baseline returns insufficient evidence instead of a
manufactured narrative; a real incident still explains (recall). Skipped without
Postgres."""
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
SCOPE = "eval:abstention-gate-test"
INSUFFICIENT = "Insufficient evidence"


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


def _errors(db, n, start, span_s):
    """Seed ``n`` error logs for 'cart' evenly across ``[start, start+span_s)``."""
    from src.db.models import LogEntry

    step = span_s / max(n, 1)
    for i in range(n):
        db.add(LogEntry(
            timestamp=start + timedelta(seconds=i * step), service="cart", level="error",
            raw_message="boom", normalized_message="boom", fingerprint="cart-boom", scope=SCOPE,
        ))


def _run(db, enabled, monkeypatch):
    from src.config import reload_settings
    from src.core.explain.summarizer import explain_window

    monkeypatch.setenv("ABSTENTION_ENABLED", "true" if enabled else "false")
    reload_settings()
    try:
        return explain_window(
            db=db, window_start=INJECT, window_end=WINDOW_END,
            no_llm=True, baseline_window_str="300s", scope=SCOPE,
        )
    finally:
        monkeypatch.delenv("ABSTENTION_ENABLED", raising=False)
        reload_settings()


def test_gate_abstains_on_healthy_window(db_session, monkeypatch):
    # Errors at a constant rate in baseline [INJECT-300, INJECT) and incident
    # [INJECT, INJECT+600): elevated volume but NO change vs baseline -> healthy.
    _errors(db_session, 100, INJECT - timedelta(seconds=300), 300)  # 0.33/s baseline
    _errors(db_session, 200, INJECT, 600)                            # 0.33/s incident
    db_session.flush()

    # Off (default): the loud-but-flat cluster still yields a narrative.
    off = _run(db_session, False, monkeypatch)
    assert INSUFFICIENT not in off.summary_text
    assert off.primary_cluster is not None

    # On: the gate sees ~zero error-rate elevation -> insufficient evidence.
    on = _run(db_session, True, monkeypatch)
    assert INSUFFICIENT in on.summary_text
    assert on.confidence == "low"


def test_gate_explains_real_incident(db_session, monkeypatch):
    # Errors ONLY in the incident window (novel vs a quiet baseline) -> a real
    # incident: the gate must not suppress it (recall).
    _errors(db_session, 200, INJECT, 600)  # 0.33/s incident, ~0 baseline
    db_session.flush()

    on = _run(db_session, True, monkeypatch)
    assert INSUFFICIENT not in on.summary_text
    assert on.primary_cluster is not None
    assert "cart" in on.services_affected
