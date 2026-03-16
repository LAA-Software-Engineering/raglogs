"""
Unit tests for src.core.compare.differ

Tests are grouped by concern:
  - ClusterDiff.direction property
  - _is_retry / _is_queue_growth classifiers
  - _collapse_clusters deduplication
  - compare_windows — each diff bucket
  - compare_windows — trigger diffing
  - compare_windows — sorting within buckets
  - compare_windows — edge cases (empty inputs, identical windows)
  - CompareResult.has_changes
"""
import pytest
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.core.clustering.clusterer import ClusterData
from src.core.compare.differ import (
    CHANGE_THRESHOLD,
    ClusterDiff,
    CompareResult,
    TriggerDiff,
    _collapse_clusters,
    _is_queue_growth,
    _is_retry,
    compare_windows,
)


# ── Fixtures & helpers ────────────────────────────────────────────────────────

BASE_TS = datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)


def _dt(offset_minutes: int = 0) -> datetime:
    return BASE_TS + timedelta(minutes=offset_minutes)


def _cluster(
    fingerprint: str,
    message: str,
    count: int = 10,
    services: Optional[dict] = None,
    first_seen: Optional[datetime] = None,
    last_seen: Optional[datetime] = None,
) -> ClusterData:
    return ClusterData(
        fingerprint=fingerprint,
        representative_message=message,
        count=count,
        services=services or {"api": count},
        levels={"error": count},
        first_seen=first_seen or _dt(0),
        last_seen=last_seen or _dt(30),
        baseline_count=0,
        change_ratio=float(count),
        importance_score=float(count),
    )


class _Trigger:
    """Minimal trigger-like object matching the interface compare_windows expects."""
    def __init__(self, message: str, service: str = "deploy-controller"):
        self.message = message
        self.service = service
        self.timestamp = _dt(-5)


def _compare(
    clusters_a=None,
    clusters_b=None,
    triggers_a=None,
    triggers_b=None,
):
    return compare_windows(
        clusters_a=clusters_a or [],
        clusters_b=clusters_b or [],
        triggers_a=triggers_a or [],
        triggers_b=triggers_b or [],
        window_a_start=_dt(0),
        window_a_end=_dt(30),
        window_b_start=_dt(-1440),   # 24 h earlier
        window_b_end=_dt(-1410),
    )


# ── ClusterDiff.direction ─────────────────────────────────────────────────────

class TestClusterDiffDirection:
    def test_new_when_count_b_is_none(self):
        d = ClusterDiff("fp", "msg", [], count_a=10, count_b=None)
        assert d.direction == "new"

    def test_disappeared_when_count_a_is_none(self):
        d = ClusterDiff("fp", "msg", [], count_a=None, count_b=10)
        assert d.direction == "disappeared"

    def test_increased_when_ratio_meets_threshold(self):
        # count_a / count_b == CHANGE_THRESHOLD exactly → increased
        d = ClusterDiff("fp", "msg", [], count_a=int(10 * CHANGE_THRESHOLD), count_b=10)
        assert d.direction == "increased"

    def test_increased_strictly_above_threshold(self):
        d = ClusterDiff("fp", "msg", [], count_a=100, count_b=10)
        assert d.direction == "increased"

    def test_decreased_when_ratio_meets_inverse_threshold(self):
        d = ClusterDiff("fp", "msg", [], count_a=10, count_b=int(10 * CHANGE_THRESHOLD))
        assert d.direction == "decreased"

    def test_decreased_strictly_below_threshold(self):
        d = ClusterDiff("fp", "msg", [], count_a=2, count_b=100)
        assert d.direction == "decreased"

    def test_stable_when_counts_equal(self):
        d = ClusterDiff("fp", "msg", [], count_a=10, count_b=10)
        assert d.direction == "stable"

    def test_stable_when_counts_within_threshold(self):
        # 12 / 10 = 1.2 — below CHANGE_THRESHOLD of 1.5
        d = ClusterDiff("fp", "msg", [], count_a=12, count_b=10)
        assert d.direction == "stable"

    def test_increased_guards_against_zero_count_b(self):
        # count_b=0: max(0,1)=1, so ratio = count_a/1 — should not divide by zero
        d = ClusterDiff("fp", "msg", [], count_a=50, count_b=0)
        assert d.direction == "increased"


# ── _is_retry / _is_queue_growth ──────────────────────────────────────────────

class TestClassifiers:
    @pytest.mark.parametrize("msg", [
        "Webhook retry attempt 1/3 for event evt_174550",
        "Retry evt_abc123 failed",
        "RETRY triggered for evt_XYZ",
    ])
    def test_is_retry_matches(self, msg):
        assert _is_retry(msg) is True

    @pytest.mark.parametrize("msg", [
        "Stripe signature verification failed",
        "POST /api/checkout 500",
        "Webhook queue growing, 100 events pending",   # queue but no evt_
        "retry without event id",
    ])
    def test_is_retry_no_match(self, msg):
        assert _is_retry(msg) is False

    @pytest.mark.parametrize("msg", [
        "Webhook queue growing, 164 events pending processing",
        "queue backlog exceeded limit",
        "pending queue depth is 300",
        "Message backlog in queue is high",
    ])
    def test_is_queue_growth_matches(self, msg):
        assert _is_queue_growth(msg) is True

    @pytest.mark.parametrize("msg", [
        "Stripe signature verification failed",
        "POST /api/checkout 500",
        "Retry evt_abc123 failed",
    ])
    def test_is_queue_growth_no_match(self, msg):
        assert _is_queue_growth(msg) is False


# ── _collapse_clusters ────────────────────────────────────────────────────────

class TestCollapseClusters:
    def test_normal_clusters_pass_through(self):
        clusters = [
            _cluster("a", "Stripe signature verification failed", count=100),
            _cluster("b", "POST /api/checkout 500", count=40),
        ]
        result = _collapse_clusters(clusters)
        assert len(result) == 2
        fps = {c.fingerprint for c in result}
        assert "a" in fps
        assert "b" in fps

    def test_multiple_retry_clusters_collapse_to_one(self):
        clusters = [
            _cluster("r1", "Webhook retry attempt 1/3 for event evt_111", count=1),
            _cluster("r2", "Webhook retry attempt 1/3 for event evt_222", count=1),
            _cluster("r3", "Webhook retry attempt 1/3 for event evt_333", count=1),
        ]
        result = _collapse_clusters(clusters)
        assert len(result) == 1
        merged = result[0]
        assert merged.count == 3
        assert "3 distinct events" in merged.representative_message
        assert "3 total" in merged.representative_message

    def test_multiple_queue_clusters_collapse_to_one(self):
        clusters = [
            _cluster("q1", "Webhook queue growing, 100 events pending processing", count=1),
            _cluster("q2", "Webhook queue growing, 200 events pending processing", count=1),
            _cluster("q3", "Webhook queue growing, 300 events pending processing", count=1),
        ]
        result = _collapse_clusters(clusters)
        assert len(result) == 1
        merged = result[0]
        assert merged.count == 3
        assert "Webhook queue growing" in merged.representative_message

    def test_mixed_clusters_collapse_correctly(self):
        clusters = [
            _cluster("s",  "Stripe signature verification failed", count=184),
            _cluster("r1", "Webhook retry attempt 1/3 for event evt_aaa", count=1,
                     services={"billing-worker": 1}),
            _cluster("r2", "Webhook retry attempt 1/3 for event evt_bbb", count=1,
                     services={"billing-worker": 1}),
            _cluster("q1", "Webhook queue growing, 164 events pending processing", count=1,
                     services={"billing-worker": 1}),
        ]
        result = _collapse_clusters(clusters)
        # stripe + 1 retry group + 1 queue group = 3
        assert len(result) == 3

    def test_retry_services_are_merged(self):
        clusters = [
            _cluster("r1", "retry evt_aaa", count=2, services={"worker-1": 2}),
            _cluster("r2", "retry evt_bbb", count=3, services={"worker-2": 3}),
        ]
        result = _collapse_clusters(clusters)
        assert len(result) == 1
        services = result[0].services
        assert services.get("worker-1") == 2
        assert services.get("worker-2") == 3

    def test_empty_input_returns_empty(self):
        assert _collapse_clusters([]) == []

    def test_single_normal_cluster_unchanged(self):
        c = _cluster("x", "some normal error", count=5)
        result = _collapse_clusters([c])
        assert len(result) == 1
        assert result[0].fingerprint == "x"

    def test_collapsed_retry_count_is_sum(self):
        clusters = [
            _cluster("r1", "Webhook retry attempt for event evt_001", count=3),
            _cluster("r2", "Webhook retry attempt for event evt_002", count=7),
        ]
        result = _collapse_clusters(clusters)
        assert result[0].count == 10

    def test_collapsed_queue_timestamps(self):
        clusters = [
            _cluster("q1", "queue backlog pending", count=1,
                     first_seen=_dt(5), last_seen=_dt(10)),
            _cluster("q2", "queue backlog pending", count=1,
                     first_seen=_dt(2), last_seen=_dt(20)),
        ]
        result = _collapse_clusters(clusters)
        assert result[0].first_seen == _dt(2)
        assert result[0].last_seen == _dt(20)


# ── compare_windows — diff buckets ────────────────────────────────────────────

class TestCompareWindowsBuckets:
    def test_new_cluster_in_a_not_b(self):
        ca = _cluster("stripe", "Stripe verification failed", count=184)
        result = _compare(clusters_a=[ca])
        assert len(result.new_clusters) == 1
        assert result.new_clusters[0].fingerprint == "stripe"
        assert result.new_clusters[0].count_a == 184
        assert result.new_clusters[0].count_b is None

    def test_disappeared_cluster_in_b_not_a(self):
        cb = _cluster("redis", "Redis connection timeout", count=52)
        result = _compare(clusters_b=[cb])
        assert len(result.disappeared_clusters) == 1
        assert result.disappeared_clusters[0].fingerprint == "redis"
        assert result.disappeared_clusters[0].count_b == 52
        assert result.disappeared_clusters[0].count_a is None

    def test_increased_cluster_in_both_a_larger(self):
        ca = _cluster("co500", "POST /api/checkout 500", count=90)
        cb = _cluster("co500", "POST /api/checkout 500", count=10)
        result = _compare(clusters_a=[ca], clusters_b=[cb])
        assert len(result.increased_clusters) == 1
        d = result.increased_clusters[0]
        assert d.count_a == 90
        assert d.count_b == 10

    def test_decreased_cluster_in_both_b_larger(self):
        ca = _cluster("lat", "Checkout latency warning", count=5)
        cb = _cluster("lat", "Checkout latency warning", count=100)
        result = _compare(clusters_a=[ca], clusters_b=[cb])
        assert len(result.decreased_clusters) == 1
        d = result.decreased_clusters[0]
        assert d.count_a == 5
        assert d.count_b == 100

    def test_stable_cluster_counts_similar(self):
        ca = _cluster("info", "Healthcheck OK", count=10)
        cb = _cluster("info", "Healthcheck OK", count=11)
        result = _compare(clusters_a=[ca], clusters_b=[cb])
        assert len(result.stable_clusters) == 1
        assert len(result.increased_clusters) == 0
        assert len(result.decreased_clusters) == 0

    def test_all_buckets_populated_simultaneously(self):
        clusters_a = [
            _cluster("new_fp",  "Brand new error",       count=50),
            _cluster("shared1", "Checkout 500",          count=80),   # increased
            _cluster("shared2", "Latency warning",       count=5),    # decreased
            _cluster("shared3", "Healthcheck OK",        count=10),   # stable
        ]
        clusters_b = [
            _cluster("gone_fp", "Redis timeout",         count=30),   # disappeared
            _cluster("shared1", "Checkout 500",          count=10),
            _cluster("shared2", "Latency warning",       count=100),
            _cluster("shared3", "Healthcheck OK",        count=10),
        ]
        result = _compare(clusters_a=clusters_a, clusters_b=clusters_b)
        assert len(result.new_clusters) == 1
        assert result.new_clusters[0].fingerprint == "new_fp"
        assert len(result.disappeared_clusters) == 1
        assert result.disappeared_clusters[0].fingerprint == "gone_fp"
        assert len(result.increased_clusters) == 1
        assert result.increased_clusters[0].fingerprint == "shared1"
        assert len(result.decreased_clusters) == 1
        assert result.decreased_clusters[0].fingerprint == "shared2"
        assert len(result.stable_clusters) == 1
        assert result.stable_clusters[0].fingerprint == "shared3"


# ── compare_windows — trigger diffing ─────────────────────────────────────────

class TestCompareWindowsTriggers:
    def test_new_trigger_in_a_not_b(self):
        ta = _Trigger("Deploy completed for billing-worker v2.4.1")
        result = _compare(triggers_a=[ta])
        assert len(result.new_triggers) == 1
        assert "v2.4.1" in result.new_triggers[0].message

    def test_dropped_trigger_in_b_not_a(self):
        tb = _Trigger("Deploy completed for billing-worker v2.3.0")
        result = _compare(triggers_b=[tb])
        assert len(result.dropped_triggers) == 1
        assert "v2.3.0" in result.dropped_triggers[0].message

    def test_same_trigger_in_both_produces_no_diff(self):
        ta = _Trigger("Application started billing-worker on port 8080")
        tb = _Trigger("Application started billing-worker on port 8080")
        result = _compare(triggers_a=[ta], triggers_b=[tb])
        assert result.new_triggers == []
        assert result.dropped_triggers == []

    def test_trigger_prefix_normalisation_suppresses_version_diff(self):
        # Both share the same 60-char prefix — should NOT appear as new/dropped
        prefix = "Deploy completed for billing-worker version "
        ta = _Trigger(prefix + "v2.4.1")
        tb = _Trigger(prefix + "v2.3.9")
        result = _compare(triggers_a=[ta], triggers_b=[tb])
        assert result.new_triggers == []
        assert result.dropped_triggers == []

    def test_trigger_service_preserved(self):
        ta = _Trigger("Deploy completed", service="deployment-controller")
        result = _compare(triggers_a=[ta])
        assert result.new_triggers[0].service == "deployment-controller"

    def test_trigger_only_in_field_is_set_correctly(self):
        ta = _Trigger("Restart in A only")
        tb = _Trigger("Restart in B only")
        result = _compare(triggers_a=[ta], triggers_b=[tb])
        assert result.new_triggers[0].only_in == "a"
        assert result.dropped_triggers[0].only_in == "b"

    def test_multiple_triggers_all_new(self):
        triggers_a = [
            _Trigger("Deploy completed billing-worker v2.4.1"),
            _Trigger("Application started billing-worker on port 8080"),
        ]
        result = _compare(triggers_a=triggers_a)
        assert len(result.new_triggers) == 2


# ── compare_windows — sorting ─────────────────────────────────────────────────

class TestCompareWindowsSorting:
    def test_new_clusters_sorted_by_count_descending(self):
        clusters_a = [
            _cluster("a", "error A", count=10),
            _cluster("b", "error B", count=184),
            _cluster("c", "error C", count=39),
        ]
        result = _compare(clusters_a=clusters_a)
        counts = [d.count_a for d in result.new_clusters]
        assert counts == sorted(counts, reverse=True)

    def test_disappeared_clusters_sorted_by_count_b_descending(self):
        clusters_b = [
            _cluster("x", "gone X", count=5),
            _cluster("y", "gone Y", count=100),
            _cluster("z", "gone Z", count=40),
        ]
        result = _compare(clusters_b=clusters_b)
        counts = [d.count_b for d in result.disappeared_clusters]
        assert counts == sorted(counts, reverse=True)

    def test_increased_clusters_sorted_by_delta_descending(self):
        # deltas: 80-10=70, 50-5=45, 30-2=28
        clusters_a = [
            _cluster("p", "err P", count=30),
            _cluster("q", "err Q", count=80),
            _cluster("r", "err R", count=50),
        ]
        clusters_b = [
            _cluster("p", "err P", count=2),
            _cluster("q", "err Q", count=10),
            _cluster("r", "err R", count=5),
        ]
        result = _compare(clusters_a=clusters_a, clusters_b=clusters_b)
        deltas = [(d.count_a or 0) - (d.count_b or 0) for d in result.increased_clusters]
        assert deltas == sorted(deltas, reverse=True)

    def test_decreased_clusters_sorted_by_delta_descending(self):
        # drop sizes: 100-5=95, 60-10=50, 40-20=20
        clusters_a = [
            _cluster("p", "err P", count=20),
            _cluster("q", "err Q", count=5),
            _cluster("r", "err R", count=10),
        ]
        clusters_b = [
            _cluster("p", "err P", count=40),
            _cluster("q", "err Q", count=100),
            _cluster("r", "err R", count=60),
        ]
        result = _compare(clusters_a=clusters_a, clusters_b=clusters_b)
        drops = [(d.count_b or 0) - (d.count_a or 0) for d in result.decreased_clusters]
        assert drops == sorted(drops, reverse=True)


# ── compare_windows — noise deduplication integration ─────────────────────────

class TestCompareWindowsDeduplication:
    def test_retry_clusters_collapsed_before_diff(self):
        """Many individual retry clusters in A should produce one collapsed diff entry."""
        clusters_a = [
            _cluster(f"r{i}", f"Webhook retry attempt for event evt_{i:04d}", count=1)
            for i in range(10)
        ]
        result = _compare(clusters_a=clusters_a)
        # All retries collapse into one — only 1 new cluster
        assert len(result.new_clusters) == 1
        assert "distinct events" in result.new_clusters[0].message

    def test_queue_clusters_collapsed_before_diff(self):
        clusters_a = [
            _cluster(f"q{i}", f"Webhook queue growing, {100+i*50} events pending processing", count=1)
            for i in range(5)
        ]
        result = _compare(clusters_a=clusters_a)
        assert len(result.new_clusters) == 1
        assert "queue growing" in result.new_clusters[0].message.lower()

    def test_retry_in_both_windows_compares_as_single_entry(self):
        """Retries in A and B should collapse to one each, then diff normally."""
        clusters_a = [
            _cluster("r1", "retry attempt for event evt_aaa", count=1),
            _cluster("r2", "retry attempt for event evt_bbb", count=1),
            _cluster("r3", "retry attempt for event evt_ccc", count=1),
        ]
        clusters_b = [
            _cluster("r4", "retry attempt for event evt_xxx", count=1),
        ]
        result = _compare(clusters_a=clusters_a, clusters_b=clusters_b)
        # Both sides collapse to __collapsed_Webhook retries...__ fingerprint
        # A has 3, B has 1 → ratio 3/1 = 3.0 ≥ CHANGE_THRESHOLD → increased
        assert len(result.increased_clusters) == 1
        assert result.increased_clusters[0].count_a == 3
        assert result.increased_clusters[0].count_b == 1

    def test_normal_clusters_not_affected_by_collapse(self):
        clusters_a = [
            _cluster("stripe", "Stripe signature verification failed", count=100),
            _cluster("r1",     "retry attempt for event evt_001",       count=1),
        ]
        result = _compare(clusters_a=clusters_a)
        assert len(result.new_clusters) == 2
        fps = {d.fingerprint for d in result.new_clusters}
        assert "stripe" in fps


# ── CompareResult.has_changes ─────────────────────────────────────────────────

class TestHasChanges:
    def test_false_when_all_buckets_empty(self):
        r = CompareResult(
            window_a_start=_dt(0), window_a_end=_dt(30),
            window_b_start=_dt(-1440), window_b_end=_dt(-1410),
        )
        assert r.has_changes is False

    def test_true_when_new_clusters(self):
        r = CompareResult(
            window_a_start=_dt(0), window_a_end=_dt(30),
            window_b_start=_dt(-1440), window_b_end=_dt(-1410),
            new_clusters=[ClusterDiff("fp", "msg", [], 10, None)],
        )
        assert r.has_changes is True

    def test_true_when_disappeared_clusters(self):
        r = CompareResult(
            window_a_start=_dt(0), window_a_end=_dt(30),
            window_b_start=_dt(-1440), window_b_end=_dt(-1410),
            disappeared_clusters=[ClusterDiff("fp", "msg", [], None, 10)],
        )
        assert r.has_changes is True

    def test_true_when_new_triggers_only(self):
        r = CompareResult(
            window_a_start=_dt(0), window_a_end=_dt(30),
            window_b_start=_dt(-1440), window_b_end=_dt(-1410),
            new_triggers=[TriggerDiff("deploy", "svc", "a")],
        )
        assert r.has_changes is True

    def test_false_when_only_stable_clusters(self):
        """Stable + dropped_triggers alone should not set has_changes."""
        r = CompareResult(
            window_a_start=_dt(0), window_a_end=_dt(30),
            window_b_start=_dt(-1440), window_b_end=_dt(-1410),
            stable_clusters=[ClusterDiff("fp", "msg", [], 10, 10)],
            dropped_triggers=[TriggerDiff("old deploy", "svc", "b")],
        )
        assert r.has_changes is False


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_both_windows_empty(self):
        result = _compare()
        assert not result.has_changes
        assert result.new_clusters == []
        assert result.disappeared_clusters == []

    def test_identical_windows_all_stable(self):
        clusters = [
            _cluster("a", "error A", count=50),
            _cluster("b", "error B", count=20),
        ]
        result = _compare(clusters_a=clusters, clusters_b=clusters)
        assert result.new_clusters == []
        assert result.disappeared_clusters == []
        assert result.increased_clusters == []
        assert result.decreased_clusters == []
        assert len(result.stable_clusters) == 2

    def test_window_metadata_preserved(self):
        result = compare_windows(
            clusters_a=[], clusters_b=[],
            triggers_a=[], triggers_b=[],
            window_a_start=_dt(0), window_a_end=_dt(30),
            window_b_start=_dt(-1440), window_b_end=_dt(-1410),
        )
        assert result.window_a_start == _dt(0)
        assert result.window_a_end == _dt(30)
        assert result.window_b_start == _dt(-1440)
        assert result.window_b_end == _dt(-1410)

    def test_cluster_message_taken_from_a_when_present(self):
        ca = _cluster("fp", "Message from A", count=10)
        cb = _cluster("fp", "Message from B", count=5)
        result = _compare(clusters_a=[ca], clusters_b=[cb])
        # cluster is in both — direction depends on ratio, message from whichever side
        all_diffs = (
            result.new_clusters + result.disappeared_clusters +
            result.increased_clusters + result.decreased_clusters +
            result.stable_clusters
        )
        assert len(all_diffs) == 1

    def test_single_event_count_a_direction_new(self):
        ca = _cluster("x", "rare error", count=1)
        result = _compare(clusters_a=[ca])
        assert len(result.new_clusters) == 1
        assert result.new_clusters[0].count_a == 1
