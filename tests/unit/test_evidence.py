"""
Tests for src.core.explain.evidence._build_evidence_items

Focused on the derived-text logic: queue-growth reformat, secondary
ordering and phrasing, trigger timing correlation, baseline messaging,
and the no-primary fallback. Does not require a database.
"""
import pytest
from datetime import datetime, timezone, timedelta

from src.core.clustering.clusterer import ClusterData
from src.core.explain.evidence import (
    TriggerCandidate,
    _build_evidence_items,
    _services_str,
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
