"""Integration test for build_service_graph — the trace_spans DB layer (#82 T1).
Skipped without Postgres."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SCOPE = "eval:linkage-test"


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


def test_build_service_graph_from_persisted_spans(db_session) -> None:
    from src.core.ingestion.telemetry import ParsedSpan, persist_spans
    from src.core.rca.linkage import build_service_graph

    # frontend -> cart -> redis
    spans = [
        ParsedSpan(trace_id="t", span_id="f", parent_span_id=None, service="frontend", start_time=T0),
        ParsedSpan(trace_id="t", span_id="c", parent_span_id="f", service="cart", start_time=T0 + timedelta(seconds=1)),
        ParsedSpan(trace_id="t", span_id="r", parent_span_id="c", service="redis", start_time=T0 + timedelta(seconds=2)),
    ]
    persist_spans(db_session, spans, scope=SCOPE)
    db_session.flush()

    g = build_service_graph(db_session, SCOPE, T0 - timedelta(seconds=60), T0 + timedelta(seconds=60))
    assert g.services == {"frontend", "cart", "redis"}
    assert g.linked("frontend", "redis")  # transitive
    assert not g.linked("cart", "not-a-service")

    # scope isolation: a different scope sees nothing
    empty = build_service_graph(db_session, "eval:other", T0 - timedelta(seconds=60), T0 + timedelta(seconds=60))
    assert empty.empty
