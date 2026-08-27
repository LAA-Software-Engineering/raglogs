"""Integration coverage for trigger-search determinism and recall (#76).

``find_trigger_candidates`` used to run ``select(LogEntry).limit(5000)`` with
no ``ORDER BY``. Postgres is free to return any 5000 matching rows for a
query shaped like that, so on any window with more candidates than the cap
the same ``explain`` call could return different trigger candidates — and a
different confidence label — from one run to the next.

Ordering by ``(timestamp, id)`` before the cap fixes determinism, but on its
own does *not* fix recall: the cap still applied to every log row in range,
not to trigger-shaped rows, so on a busy scope the earliest 5000 rows in
range can all be non-trigger noise, and a real trigger occurring later in
range is silently and *deterministically* dropped every time — a regression
from "sometimes missed" to "always missed" for that shape of incident. The
fix filters to ``TRIGGER_PATTERNS`` matches in SQL before ordering/capping,
so the cap bounds trigger candidates rather than log volume.

Round 2: matching ``raw_message`` in that SQL filter unconditionally
reopened the same bug one column over. ``is_trigger_message()`` only reads
``raw_message`` when ``normalized_message`` is empty -- otherwise it reads
``normalized_message`` alone. So a row with a benign ``normalized_message``
but a raw JSON blob that incidentally contains trigger-shaped text in some
other field (an error/detail field, a stack trace) matched the SQL filter as
a false positive, consumed a cap slot, and was then correctly rejected by
``is_trigger_message()`` -- but too late, since the cap had already been
spent on it instead of the real trigger. The fix gates the ``raw_message``
branch behind ``normalized_message`` being empty/null, mirroring the Python
fallback exactly.

Skipped without a live Postgres (CI has no DB by default); the query-shape
assertions that don't need a real database live in tests/unit/test_evidence.py.
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

# Comfortably over the find_trigger_candidates row cap (5000).
NOISE_ROW_COUNT = 6000


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


def _heartbeat_rows(scope: str, start: datetime, end: datetime, count: int) -> list:
    from src.db.models import LogEntry

    span_seconds = (end - start).total_seconds()
    return [
        LogEntry(
            id=uuid.uuid4(),
            timestamp=start + timedelta(seconds=(span_seconds * i) / count),
            service="api",
            level="info",
            normalized_message=f"heartbeat {i}",
            source_adapter="file",
            scope=scope,
        )
        for i in range(1, count + 1)
    ]


def _raw_json_false_positive_rows(scope: str, start: datetime, end: datetime, count: int) -> list:
    """Rows with a benign normalized_message but a raw JSON blob that
    incidentally contains trigger-shaped text in an unrelated field --
    the false-positive-at-SQL-layer shape from PR #89 review round 2."""
    import orjson

    from src.db.models import LogEntry

    span_seconds = (end - start).total_seconds()
    rows = []
    for i in range(1, count + 1):
        raw = orjson.dumps(
            {
                "level": "info",
                "message": f"heartbeat {i}",
                "detail": "token expired notice suppressed, will not retry",
            }
        ).decode()
        rows.append(
            LogEntry(
                id=uuid.uuid4(),
                timestamp=start + timedelta(seconds=(span_seconds * i) / count),
                service="api",
                level="info",
                normalized_message=f"heartbeat {i}",  # benign -- Python would reject this
                raw_message=raw,
                source_adapter="file",
                scope=scope,
            )
        )
    return rows


def test_two_calls_over_a_large_window_return_identical_candidates(db_session) -> None:
    """Determinism: same window, same data, two calls, same result. This is
    the property a plain unordered LIMIT could not guarantee."""
    from src.core.explain.evidence import find_trigger_candidates
    from src.db.models import LogEntry

    scope = f"test-trigger-determinism-{uuid.uuid4().hex[:8]}"
    window_start = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(hours=1)
    lookback_minutes = 10
    search_start = window_start - timedelta(minutes=lookback_minutes)

    trigger_entry = LogEntry(
        id=uuid.uuid4(),
        timestamp=search_start + timedelta(minutes=3),
        service="deployment-controller",
        level="info",
        normalized_message="Deploy completed for billing-worker v2.4.1",
        source_adapter="file",
        scope=scope,
    )

    db_session.add_all(_heartbeat_rows(scope, search_start, window_end, NOISE_ROW_COUNT))
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


def test_trigger_after_5000_earlier_noise_rows_is_still_found(db_session) -> None:
    """Recall regression guard.

    Concrete scenario from PR #89 review: window_start=12:00, 10m lookback
    (search_start=11:50), 6000 heartbeats packed into 11:50-11:58, then a
    real "Deploy completed" trigger at 11:59 -- chronologically the 6001st
    event in range. ORDER BY timestamp ASC LIMIT 5000 alone always excludes
    this row (it is never among the earliest 5000 by timestamp), which
    converts the pre-fix "sometimes misses on an arbitrary Postgres subset"
    bug into an "always misses" bug for any incident shaped like this one.
    The fix (filter to TRIGGER_PATTERNS matches in SQL before the cap) must
    find it regardless of how much earlier noise preceded it.
    """
    from src.core.explain.evidence import find_trigger_candidates
    from src.db.models import LogEntry

    scope = f"test-trigger-recall-{uuid.uuid4().hex[:8]}"
    window_start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(hours=1)
    lookback_minutes = 10
    search_start = window_start - timedelta(minutes=lookback_minutes)  # 11:50

    # 6000 heartbeats packed into the first 8 of the 10 lookback minutes.
    noise_end = search_start + timedelta(minutes=8)  # 11:58
    db_session.add_all(_heartbeat_rows(scope, search_start, noise_end, NOISE_ROW_COUNT))
    db_session.flush()

    trigger_entry = LogEntry(
        id=uuid.uuid4(),
        timestamp=window_start - timedelta(minutes=1),  # 11:59 -- after all the noise
        service="deployment-controller",
        level="info",
        normalized_message="Deploy completed for billing-worker v2.4.1",
        source_adapter="file",
        scope=scope,
    )
    db_session.add(trigger_entry)
    db_session.flush()

    candidates = find_trigger_candidates(
        db_session, window_start, window_end, lookback_minutes=lookback_minutes, scope=scope
    )

    assert any("Deploy completed for billing-worker" in c.message for c in candidates), (
        f"trigger not found among {len(candidates)} candidate(s) despite being in range — "
        "the row cap is bounding log volume again instead of trigger candidates"
    )


def test_raw_json_false_positives_do_not_starve_the_cap(db_session) -> None:
    """PR #89 review round 2's exact scenario: 6000 rows precede the deploy,
    each with a benign normalized_message ("heartbeat N") that
    is_trigger_message() would reject, but a raw JSON blob containing "token
    expired" in an unrelated field. Matching raw_message unconditionally in
    SQL would count all 6000 as trigger candidates, spend the entire row cap
    on them, and never reach the real deploy trigger -- even though Python
    would have rejected every one of those 6000 rows itself, because it never
    reads raw_message when normalized_message is populated. Confirmed this
    fails without the empty-normalized_message gate and passes with it.
    """
    from src.core.explain.evidence import find_trigger_candidates
    from src.db.models import LogEntry

    scope = f"test-trigger-raw-false-positive-{uuid.uuid4().hex[:8]}"
    window_start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(hours=1)
    lookback_minutes = 10
    search_start = window_start - timedelta(minutes=lookback_minutes)  # 11:50
    noise_end = search_start + timedelta(minutes=8)  # 11:58

    db_session.add_all(
        _raw_json_false_positive_rows(scope, search_start, noise_end, NOISE_ROW_COUNT)
    )
    db_session.flush()

    trigger_entry = LogEntry(
        id=uuid.uuid4(),
        timestamp=window_start - timedelta(minutes=1),  # 11:59
        service="deployment-controller",
        level="info",
        normalized_message="Deploy completed for billing-worker v2.4.1",
        source_adapter="file",
        scope=scope,
    )
    db_session.add(trigger_entry)
    db_session.flush()

    candidates = find_trigger_candidates(
        db_session, window_start, window_end, lookback_minutes=lookback_minutes, scope=scope
    )

    assert any("Deploy completed for billing-worker" in c.message for c in candidates), (
        f"trigger not found among {len(candidates)} candidate(s) — raw_message false "
        "positives (benign normalized_message, trigger-shaped raw JSON) starved the cap"
    )


def test_widening_lookback_recovers_a_slow_burn_trigger(db_session) -> None:
    """The stated purpose of TRIGGER_LOOKBACK_MINUTES (#76): a trigger well
    before window_start should be found by widening the lookback, without the
    intervening noise crowding it out of the row cap."""
    from src.core.explain.evidence import find_trigger_candidates
    from src.db.models import LogEntry

    scope = f"test-trigger-lookback-{uuid.uuid4().hex[:8]}"
    window_start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(hours=1)

    # Trigger 25 minutes before window_start -- outside the default 10m
    # lookback, inside a widened 30m one.
    trigger_time = window_start - timedelta(minutes=25)
    trigger_entry = LogEntry(
        id=uuid.uuid4(),
        timestamp=trigger_time,
        service="deployment-controller",
        level="info",
        normalized_message="Deploy completed for billing-worker v2.4.1",
        source_adapter="file",
        scope=scope,
    )
    db_session.add_all(
        _heartbeat_rows(
            scope, window_start - timedelta(minutes=30), window_end, NOISE_ROW_COUNT
        )
    )
    db_session.flush()
    db_session.add(trigger_entry)
    db_session.flush()

    missed = find_trigger_candidates(
        db_session, window_start, window_end, lookback_minutes=10, scope=scope
    )
    assert not any("Deploy completed" in c.message for c in missed)

    found = find_trigger_candidates(
        db_session, window_start, window_end, lookback_minutes=30, scope=scope
    )
    assert any("Deploy completed for billing-worker" in c.message for c in found)


@pytest.mark.parametrize(
    "message",
    [
        "Deploy completed for billing-worker v2.4.1",
        "deployment started for api",
        "Application restarted after crash",
        "pod evicted due to memory pressure",
        "Configuration reloaded successfully",
        "Migration running: 0003_add_index",
        "Queue full: rejecting new events",
        "circuit_breaker tripped for payments-service",
        "Webhook secret changed by admin",
        "Token expired for session abc123",
        "release v3.0.0 deployed to prod",
        "Rollout completed successfully",
    ],
)
def test_every_trigger_pattern_is_found_via_the_sql_filter(db_session, message: str) -> None:
    """Regex-dialect equivalence regression guard: each TRIGGER_PATTERNS
    entry must round-trip through Postgres's ~* the same way Python's `re`
    matches it, end to end through find_trigger_candidates — not just in an
    ad hoc check against the raw operator."""
    from src.core.explain.evidence import find_trigger_candidates
    from src.db.models import LogEntry

    scope = f"test-trigger-pattern-{uuid.uuid4().hex[:8]}"
    window_start = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(hours=1)

    entry = LogEntry(
        id=uuid.uuid4(),
        timestamp=window_start - timedelta(minutes=2),
        service="some-service",
        level="info",
        normalized_message=message,
        source_adapter="file",
        scope=scope,
    )
    db_session.add(entry)
    db_session.flush()

    candidates = find_trigger_candidates(db_session, window_start, window_end, scope=scope)

    assert any(c.message == message for c in candidates), f"not found via SQL filter: {message!r}"
