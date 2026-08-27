"""Integration coverage for the trigger-search determinism fix (#76).

``find_trigger_candidates`` used to run ``select(LogEntry).limit(5000)`` with
no ``ORDER BY``. Postgres is free to return any 5000 matching rows for a
query shaped like that, so on any window with more candidates than the cap
the same ``explain`` call could return different trigger candidates — and a
different confidence label — from one run to the next, and a real trigger
near the edge of a large window could simply not be in the sampled rows.

This is skipped without a live Postgres (CI has no DB by default); the
query-shape assertions that don't need a real database live in
tests/unit/test_evidence.py.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)

# Comfortably over the find_trigger_candidates row cap (5000) without making
# the test slow. The mechanism under test (order earliest-first, then cap)
# generalizes to arbitrarily large windows — this only needs to prove the
# cap no longer drops an in-range trigger non-deterministically.
ROW_COUNT = 5500


@pytest.fixture
def db_session():
    from src.db.models import Base
    from src.db.session import check_connection, get_db, get_engine

    if not check_connection():
        pytest.skip("Cannot connect to database")

    engine = get_engine()
    from sqlalchemy import text

    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.create_all(engine)

    with get_db() as db:
        yield db


def test_trigger_at_start_of_large_window_is_found_deterministically(db_session) -> None:
    from src.core.explain.evidence import find_trigger_candidates
    from src.db.models import LogEntry

    scope = f"test-trigger-search-{uuid.uuid4().hex[:8]}"
    window_start = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(hours=1)
    lookback_minutes = 10
    search_start = window_start - timedelta(minutes=lookback_minutes)

    # The trigger sits at the very first in-range instant. With the old
    # unordered LIMIT 5000, whether Postgres happened to include this row in
    # its arbitrary subset was pure luck. With ORDER BY timestamp ASC it is
    # always among the earliest 5000 rows returned, by construction.
    trigger_entry = LogEntry(
        id=uuid.uuid4(),
        timestamp=search_start,
        service="deployment-controller",
        level="info",
        normalized_message="Deploy completed for billing-worker v2.4.1",
        source_adapter="file",
        scope=scope,
    )

    # ROW_COUNT non-trigger rows spread across the rest of the range.
    span_seconds = (window_end - search_start).total_seconds()
    filler = [
        LogEntry(
            id=uuid.uuid4(),
            timestamp=search_start + timedelta(seconds=(span_seconds * i) / ROW_COUNT),
            service="api",
            level="info",
            normalized_message=f"heartbeat {i}",
            source_adapter="file",
            scope=scope,
        )
        for i in range(1, ROW_COUNT + 1)
    ]

    # Insert the filler first and the trigger last, so its physical/insertion
    # order is decorrelated from its (earliest) timestamp order. This is the
    # arrangement that actually exercises the bug: a plain sequential scan
    # tends to return rows in roughly insertion order when there's no ORDER
    # BY, which would let a naive "limit(5000), no order_by" query silently
    # drop this trigger every time even though it belongs in the window.
    # (Verified against the pre-fix query on this exact dataset: 0/5000
    # returned rows contained it.)
    db_session.add_all(filler)
    db_session.flush()
    db_session.add(trigger_entry)
    db_session.flush()

    first = find_trigger_candidates(
        db_session, window_start, window_end, lookback_minutes=lookback_minutes, scope=scope
    )
    second = find_trigger_candidates(
        db_session, window_start, window_end, lookback_minutes=lookback_minutes, scope=scope
    )

    assert [c.message for c in first] == [c.message for c in second]
    assert any("Deploy completed for billing-worker" in c.message for c in first)
