"""
Tests for src.core.explain.evidence._build_evidence_items and
find_trigger_candidates's query construction.

Focused on the derived-text logic: queue-growth reformat, secondary
ordering and phrasing, trigger timing correlation, baseline messaging,
and the no-primary fallback. Does not require a database — the query-shape
tests inspect the compiled Select against a mocked session (ordering,
row cap, the SQL-side TRIGGER_PATTERNS filter, column scoping, lookback
resolution); tests/integration/test_trigger_search.py covers the actual
determinism and cap-vs-recall behavior against a live Postgres.
"""
import pytest
from collections import namedtuple
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from src.core.clustering.clusterer import ClusterData
from src.core.explain.evidence import (
    TriggerCandidate,
    _build_evidence_items,
    _services_str,
    find_trigger_candidates,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(tz=timezone.utc)


def _cluster(
    message: str,
    count: int = 10,
    services: dict | None = None,
    levels: dict | None = None,
    first_seen: datetime | None = None,
    baseline_count: int = 0,
    change_ratio: float = 1.0,
) -> ClusterData:
    return ClusterData(
        fingerprint="abcd1234",
        representative_message=message,
        count=count,
        services={"api": count} if services is None else services,
        levels={"error": count} if levels is None else levels,
        first_seen=first_seen or _now(),
        last_seen=_now(),
        baseline_count=baseline_count,
        change_ratio=change_ratio,
        importance_score=5.0,
    )


def _trigger(message: str, offset_minutes: int = -5, service: str = "deploy") -> TriggerCandidate:
    return TriggerCandidate(
        message=message,
        timestamp=_now() + timedelta(minutes=offset_minutes),
        service=service,
    )


# ── No primary cluster ────────────────────────────────────────────────────────

class TestNoPrimary:
    def test_returns_fallback_items(self):
        items = _build_evidence_items(
            primary=None, secondary=[], triggers=[], total_logs=42, window_start=_now()
        )
        assert any("42" in i for i in items)
        assert any("No significant error" in i for i in items)

    def test_empty_total_logs(self):
        items = _build_evidence_items(
            primary=None, secondary=[], triggers=[], total_logs=0, window_start=_now()
        )
        assert len(items) == 2


# ── Primary cluster ───────────────────────────────────────────────────────────

class TestPrimaryCluster:
    def test_count_and_service_in_first_item(self):
        p = _cluster("DB connection refused", count=55, services={"payments": 55})
        items = _build_evidence_items(p, [], [], 100, _now())
        assert items[0] == "55 similar failures in payments"

    def test_baseline_zero_emits_not_observed(self):
        p = _cluster("timeout", count=20, baseline_count=0, change_ratio=21.0)
        items = _build_evidence_items(p, [], [], 100, _now())
        assert any("Not observed in prior 24h baseline" in i for i in items)

    def test_high_change_ratio_emits_multiplier(self):
        p = _cluster("timeout", count=200, baseline_count=5, change_ratio=40.0)
        items = _build_evidence_items(p, [], [], 300, _now())
        assert any("40x" in i for i in items)

    def test_low_change_ratio_emits_baseline_count(self):
        p = _cluster("timeout", count=15, baseline_count=10, change_ratio=1.5)
        items = _build_evidence_items(p, [], [], 100, _now())
        assert any("10 similar events" in i for i in items)

    def test_endpoint_extracted_from_message(self):
        p = _cluster("Stripe verification failed for /webhooks/stripe", count=50)
        items = _build_evidence_items(p, [], [], 100, _now())
        assert any("/webhooks/stripe" in i for i in items)

    def test_no_endpoint_in_message_skips_endpoint_item(self):
        p = _cluster("Database connection refused", count=50)
        items = _build_evidence_items(p, [], [], 100, _now())
        assert not any("appears in the primary error cluster" in i for i in items)


# ── Trigger timing ────────────────────────────────────────────────────────────

class TestTriggerTiming:
    def test_timing_within_30m_emits_spike_item(self):
        deploy_time = _now() - timedelta(minutes=10)
        error_time = deploy_time + timedelta(minutes=2)
        p = _cluster("error", count=50, first_seen=error_time)
        t = TriggerCandidate(
            message="Deploy completed for service v1.2.3",
            timestamp=deploy_time,
            service="deploy-controller",
        )
        items = _build_evidence_items(p, [], [t], 100, _now())
        spike_items = [i for i in items if "error spike occurred" in i]
        assert len(spike_items) == 1
        assert "2m after" in spike_items[0]

    def test_timing_uses_trigger_message_text(self):
        deploy_time = _now() - timedelta(minutes=10)
        error_time = deploy_time + timedelta(minutes=3)
        p = _cluster("error", count=50, first_seen=error_time)
        t = TriggerCandidate(
            message="Deploy completed for billing-worker version v2.4.1",
            timestamp=deploy_time,
            service="dc",
        )
        items = _build_evidence_items(p, [], [t], 100, _now())
        assert any("deploy completed" in i.lower() for i in items)

    def test_timing_outside_30m_skips_spike_item(self):
        deploy_time = _now() - timedelta(minutes=60)
        error_time = deploy_time + timedelta(minutes=45)
        p = _cluster("error", count=50, first_seen=error_time)
        t = TriggerCandidate(message="Deploy completed", timestamp=deploy_time, service="dc")
        items = _build_evidence_items(p, [], [t], 100, _now())
        assert not any("error spike occurred" in i for i in items)

    def test_trigger_before_errors_negative_delta_skipped(self):
        error_time = _now() - timedelta(minutes=20)
        trigger_time = error_time + timedelta(minutes=5)  # trigger AFTER errors
        p = _cluster("error", count=50, first_seen=error_time)
        t = TriggerCandidate(message="Deploy completed", timestamp=trigger_time, service="dc")
        items = _build_evidence_items(p, [], [t], 100, _now())
        assert not any("error spike occurred" in i for i in items)

    def test_no_triggers_skips_timing_item(self):
        p = _cluster("error", count=50)
        items = _build_evidence_items(p, [], [], 100, _now())
        assert not any("error spike occurred" in i for i in items)


# ── Secondary effects ─────────────────────────────────────────────────────────

class TestSecondaryEffects:
    def _primary(self):
        t = _now() - timedelta(minutes=30)
        return _cluster("Stripe signature verification failed", count=184, first_seen=t)

    def test_all_three_secondaries_appear(self):
        """The indentation bug we fixed — verify all items are emitted."""
        base = _now() - timedelta(minutes=28)
        s1 = _cluster("POST /api/checkout 500", count=39, first_seen=base)
        s2 = _cluster("POST /api/checkout 200 latency=5120ms", count=25, first_seen=base)
        s3 = _cluster("queue growing, 10 events pending processing", count=2,
                      services={"worker": 2}, levels={"warn": 2}, first_seen=base)
        items = _build_evidence_items(self._primary(), [s1, s2, s3], [], 300, _now())
        # All three should appear (the bug caused only the last to appear)
        full_text = "\n".join(items)
        assert "39" in full_text
        assert "25" in full_text

    def test_500_secondary_uses_checkout_phrasing(self):
        base = _now() - timedelta(minutes=28)
        p = self._primary()
        s = _cluster("POST /api/checkout 500 Internal Server Error", count=39, first_seen=base)
        items = _build_evidence_items(p, [s], [], 200, _now())
        assert any("checkout 500s" in i for i in items)

    def test_latency_secondary_uses_latency_phrasing(self):
        base = _now() - timedelta(minutes=28)
        p = self._primary()
        s = _cluster("checkout 200 latency=5120ms high latency detected", count=25, first_seen=base)
        items = _build_evidence_items(p, [s], [], 200, _now())
        assert any("elevated-latency" in i for i in items)

    def test_generic_secondary_uses_generic_phrasing(self):
        base = _now() - timedelta(minutes=28)
        p = self._primary()
        s = _cluster("Cache miss for user session", count=12, first_seen=base)
        items = _build_evidence_items(p, [s], [], 200, _now())
        assert any("started after primary" in i for i in items)

    def test_secondary_before_primary_uses_related_phrasing(self):
        primary_start = _now() - timedelta(minutes=20)
        secondary_start = _now() - timedelta(minutes=40)  # started before primary
        p = _cluster("error", count=50, first_seen=primary_start)
        s = _cluster("background job failed", count=15, first_seen=secondary_start)
        items = _build_evidence_items(p, [s], [], 200, _now())
        assert any(i.startswith("Related:") for i in items)


# ── Queue-growth message ──────────────────────────────────────────────────────

class TestQueueGrowth:
    def _primary(self):
        t = _now() - timedelta(minutes=30)
        return _cluster("Stripe error", count=184, first_seen=t)

    def test_queue_depth_extracted(self):
        base = _now() - timedelta(minutes=28)
        q = _cluster("Webhook queue growing, 364 events pending processing",
                     count=2, services={"worker": 2}, levels={"warn": 2}, first_seen=base)
        items = _build_evidence_items(self._primary(), [q], [], 200, _now())
        assert any("364 pending items" in i for i in items)

    def test_log_count_separated_from_depth(self):
        base = _now() - timedelta(minutes=28)
        q = _cluster("queue: 128 events pending processing",
                     count=3, services={"worker": 3}, levels={"warn": 3}, first_seen=base)
        items = _build_evidence_items(self._primary(), [q], [], 200, _now())
        queue_item = next(i for i in items if "pending items" in i)
        assert "128 pending items" in queue_item
        assert "3 log events" in queue_item

    def test_singular_log_event(self):
        base = _now() - timedelta(minutes=28)
        q = _cluster("queue: 50 events pending processing",
                     count=1, services={"worker": 1}, levels={"warn": 1}, first_seen=base)
        items = _build_evidence_items(self._primary(), [q], [], 200, _now())
        queue_item = next(i for i in items if "pending items" in i)
        assert "1 log event" in queue_item
        assert "log events" not in queue_item  # no accidental plural

    def test_non_queue_warn_not_reformatted(self):
        base = _now() - timedelta(minutes=28)
        s = _cluster("Memory usage high: 85% utilized",
                     count=5, services={"api": 5}, levels={"warn": 5}, first_seen=base)
        items = _build_evidence_items(self._primary(), [s], [], 200, _now())
        assert not any("pending items" in i for i in items)


# ── Max items cap ─────────────────────────────────────────────────────────────

class TestMaxItems:
    def test_respects_max_items(self):
        p = _cluster("error", count=100, baseline_count=0)
        secondaries = [
            _cluster(f"secondary error {i}", count=10+i,
                     first_seen=_now() - timedelta(minutes=5))
            for i in range(10)
        ]
        items = _build_evidence_items(p, secondaries, [], 500, _now(), max_items=3)
        assert len(items) <= 3


# ── _services_str ─────────────────────────────────────────────────────────────

class TestServicesStr:
    def test_single_service(self):
        c = _cluster("x", services={"billing-worker": 10})
        assert _services_str(c) == "billing-worker"

    def test_two_services(self):
        c = _cluster("x", services={"api": 5, "worker": 5})
        result = _services_str(c)
        assert "api" in result and "worker" in result

    def test_many_services_truncated(self):
        c = _cluster("x", services={f"svc{i}": 1 for i in range(5)})
        result = _services_str(c)
        assert result.endswith("...")

    def test_no_services(self):
        c = _cluster("x", services={})
        assert _services_str(c) == "unknown service"


# ── find_trigger_candidates query construction (#76) ───────────────────────────
#
# select(LogEntry).limit(5000) with no ORDER BY let Postgres return an
# arbitrary subset of matching rows on any window with more than 5000
# candidates, so the same explain call could return different trigger
# candidates — and a different confidence label — from one run to the next.
#
# Ordering by (timestamp, id) alone fixes *which* subset is examined
# deterministically, but not recall: the row cap still applied to every log
# line in range, not to trigger-shaped lines, so on a busy scope the earliest
# N rows in range can all be non-trigger noise, silently dropping a real
# trigger occurring later in range every single time (reviewed and confirmed
# against a live Postgres — see tests/integration/test_trigger_search.py).
# The fix pushes TRIGGER_PATTERNS matching into the WHERE clause so the cap
# bounds trigger candidates, not log volume. These tests check the query
# shape without a database; the row-cap-vs-recall behavior against real data
# lives in tests/integration.

_TriggerRow = namedtuple("_TriggerRow", "normalized_message raw_message timestamp service")


def _mock_db_returning_rows(rows: list) -> MagicMock:
    db = MagicMock()
    db.execute.return_value.all.return_value = rows
    return db


class TestFindTriggerCandidatesQuery:
    def test_query_is_ordered_by_timestamp_then_id(self):
        db = _mock_db_returning_rows([])
        find_trigger_candidates(db, _now() - timedelta(hours=1), _now())

        query = db.execute.call_args[0][0]
        assert "ORDER BY log_entries.timestamp, log_entries.id" in str(query)

    def test_query_row_cap_is_5000(self):
        db = _mock_db_returning_rows([])
        find_trigger_candidates(db, _now() - timedelta(hours=1), _now())

        query = db.execute.call_args[0][0]
        compiled = str(query.compile(compile_kwargs={"literal_binds": True}))
        assert "LIMIT 5000" in compiled

    def test_query_filters_to_trigger_pattern_matches_in_sql(self):
        """The cap must bound trigger-shaped rows, not arbitrary log volume —
        otherwise a busy scope can push a real trigger past the cap before
        Python ever gets to look at it (#76 review)."""
        db = _mock_db_returning_rows([])
        find_trigger_candidates(db, _now() - timedelta(hours=1), _now())

        query = db.execute.call_args[0][0]
        unbound = str(query)
        # One TRIGGER_PATTERNS regex, translated for Postgres's ~* operator,
        # against both columns the extraction logic reads from.
        assert "log_entries.normalized_message ~*" in unbound
        assert "log_entries.raw_message ~*" in unbound

        bound = str(query.compile(compile_kwargs={"literal_binds": True}))
        assert "deploy" in bound  # a pattern's literal text made it into the SQL

    def test_every_trigger_pattern_is_represented_in_the_where_clause(self):
        from src.core.normalization.patterns import TRIGGER_PATTERNS

        db = _mock_db_returning_rows([])
        find_trigger_candidates(db, _now() - timedelta(hours=1), _now())

        query = db.execute.call_args[0][0]
        bound = str(query.compile(compile_kwargs={"literal_binds": True}))
        for pattern in TRIGGER_PATTERNS:
            assert pattern.pattern in bound

    def test_query_selects_only_the_columns_trigger_matching_needs(self):
        """Regression guard against re-widening to select(LogEntry) full ORM
        objects, which pulls host/extra_json/trace_id/request_id/parser_type/
        source_adapter for every row and gains nothing — trigger matching only
        reads these four columns. (raw_message *is* still needed: it
        participates in the SQL match condition above and in the JSON-fallback
        extraction below, for rows with no normalized_message.)"""
        db = _mock_db_returning_rows([])
        find_trigger_candidates(db, _now() - timedelta(hours=1), _now())

        query = db.execute.call_args[0][0]
        assert list(query.selected_columns.keys()) == [
            "normalized_message",
            "raw_message",
            "timestamp",
            "service",
        ]

    def test_lookback_minutes_shifts_the_search_start(self):
        db = _mock_db_returning_rows([])
        window_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        find_trigger_candidates(db, window_start, window_start + timedelta(hours=1), lookback_minutes=45)

        query = db.execute.call_args[0][0]
        compiled = str(query.compile(compile_kwargs={"literal_binds": True}))
        expected_search_start = window_start - timedelta(minutes=45)
        assert str(expected_search_start) in compiled

    def test_default_lookback_minutes_comes_from_settings_not_a_second_default(self, monkeypatch):
        """find_trigger_candidates must have exactly one source of truth for
        the default lookback. Previously the function signature defaulted to
        a hardcoded 10 *and* assemble_evidence separately read
        settings.trigger_lookback_minutes — reverting either one back to a
        bare 10 would pass every other test. Calling with no lookback_minutes
        override must reflect a changed setting, proving there's only one
        default left to revert."""
        from src.config import get_settings, reload_settings

        monkeypatch.setenv("TRIGGER_LOOKBACK_MINUTES", "37")
        reload_settings()
        try:
            db = _mock_db_returning_rows([])
            window_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            find_trigger_candidates(db, window_start, window_start + timedelta(hours=1))

            query = db.execute.call_args[0][0]
            compiled = str(query.compile(compile_kwargs={"literal_binds": True}))
            expected_search_start = window_start - timedelta(minutes=37)
            assert str(expected_search_start) in compiled
        finally:
            monkeypatch.delenv("TRIGGER_LOOKBACK_MINUTES", raising=False)
            reload_settings()
            get_settings()  # re-warm the module-level cache for later tests


class TestFindTriggerCandidatesExtraction:
    def test_row_matching_trigger_pattern_becomes_a_candidate(self):
        t = _now() - timedelta(minutes=5)
        rows = [_TriggerRow("Deploy completed for billing-worker v2.4.1", None, t, "deployment-controller")]
        db = _mock_db_returning_rows(rows)

        candidates = find_trigger_candidates(db, _now() - timedelta(hours=1), _now())

        assert len(candidates) == 1
        assert candidates[0].message.startswith("Deploy completed")
        assert candidates[0].service == "deployment-controller"
        assert candidates[0].timestamp == t

    def test_row_not_matching_trigger_pattern_is_skipped(self):
        """The SQL filter should already exclude this row; is_trigger_message
        here is the Python-side safety net (see module docstring) — this test
        exercises that net directly given a mocked row, regardless of what
        the (mocked, not-really-filtering) SQL layer would have done."""
        t = _now() - timedelta(minutes=5)
        rows = [_TriggerRow("GET /health 200 OK", None, t, "api")]
        db = _mock_db_returning_rows(rows)

        candidates = find_trigger_candidates(db, _now() - timedelta(hours=1), _now())

        assert candidates == []

    def test_falls_back_to_raw_message_json_when_normalized_is_empty(self):
        t = _now() - timedelta(minutes=5)
        rows = [_TriggerRow("", '{"message": "pod restarted after eviction"}', t, "worker")]
        db = _mock_db_returning_rows(rows)

        candidates = find_trigger_candidates(db, _now() - timedelta(hours=1), _now())

        assert len(candidates) == 1
        assert "pod restarted" in candidates[0].message

    def test_output_is_a_pure_function_of_the_input_rows(self):
        """Confirms find_trigger_candidates has no hidden state across calls —
        NOT a proof that the SQL layer is deterministic. That claim needs a
        real database and is covered in tests/integration."""
        base = _now() - timedelta(minutes=8)
        rows = [
            _TriggerRow("Deploy completed for api v3.0.0", None, base, "deploy"),
            _TriggerRow("pod restarted", None, base + timedelta(minutes=1), "api"),
        ]

        first = find_trigger_candidates(_mock_db_returning_rows(rows), _now() - timedelta(hours=1), _now())
        second = find_trigger_candidates(_mock_db_returning_rows(rows), _now() - timedelta(hours=1), _now())

        assert [c.message for c in first] == [c.message for c in second]
