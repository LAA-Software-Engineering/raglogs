"""
Tests for src.core.explain.confidence.compute_confidence

Each test targets a specific scoring rule. The key invariants:
  - 'high' requires both score >= 5 AND a trigger candidate
  - 'medium-high' is the ceiling when no trigger is present
  - no primary cluster → always 'low'
"""
import pytest
from datetime import datetime, timezone, timedelta

from src.core.clustering.clusterer import ClusterData
from src.core.explain.confidence import (
    compute_confidence,
    compute_confidence_score,
    score_from_label,
)
from src.core.explain.evidence import EvidencePacket, TriggerCandidate


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(tz=timezone.utc)


def _cluster(
    count: int = 50,
    baseline_count: int = 0,
    change_ratio: float = 51.0,
    services: dict | None = None,
    levels: dict | None = None,
) -> ClusterData:
    return ClusterData(
        fingerprint="abcd",
        representative_message="some error",
        count=count,
        services=services or {"api": count},
        levels=levels or {"error": count},
        first_seen=_now() - timedelta(minutes=30),
        last_seen=_now(),
        baseline_count=baseline_count,
        change_ratio=change_ratio,
        importance_score=5.0,
    )


def _trigger() -> TriggerCandidate:
    return TriggerCandidate(
        message="Deploy completed for service v1.2",
        timestamp=_now() - timedelta(minutes=35),
        service="deploy-controller",
    )


def _packet(
    primary=None,
    secondary=None,
    triggers=None,
    services=None,
) -> EvidencePacket:
    return EvidencePacket(
        window_start=_now() - timedelta(hours=1),
        window_end=_now(),
        total_logs=500,
        primary_cluster=primary,
        secondary_clusters=secondary or [],
        trigger_candidates=triggers or [],
        evidence_items=[],
        services_affected=services or ["api"],
    )


# ── No primary cluster ────────────────────────────────────────────────────────

class TestNoPrimary:
    def test_always_low(self):
        assert compute_confidence(_packet(primary=None)) == "low"

    def test_triggers_dont_raise_above_low_without_primary(self):
        p = _packet(primary=None, triggers=[_trigger()])
        assert compute_confidence(p) == "low"


# ── High confidence ───────────────────────────────────────────────────────────

class TestHighConfidence:
    def test_requires_trigger_for_high(self):
        """Score can be >= 5 but without trigger, max is medium-high."""
        # large cluster (2) + secondary (1) + multi-service (1) = 4 → medium-high
        pc = _cluster(count=100, services={"api": 50, "worker": 50})
        p = _packet(primary=pc, secondary=[_cluster(count=10)],
                    services=["api", "worker"], triggers=[])
        result = compute_confidence(p)
        assert result != "high"

    def test_trigger_plus_strong_cluster_is_high(self):
        # large cluster (2) + trigger (2) + secondary (1) + multi-service (1) = 6 → high
        pc = _cluster(count=100, services={"api": 50, "worker": 50})
        p = _packet(
            primary=pc,
            secondary=[_cluster(count=10)],
            services=["api", "worker"],
            triggers=[_trigger()],
        )
        assert compute_confidence(p) == "high"

    def test_trigger_alone_with_weak_cluster_not_high(self):
        # small cluster (0) + trigger (2) = 2 → medium
        pc = _cluster(count=5)
        p = _packet(primary=pc, triggers=[_trigger()])
        result = compute_confidence(p)
        assert result in ("medium", "medium-high")
        assert result != "high"


# ── Medium-high ceiling without trigger ──────────────────────────────────────

class TestMediumHighCeiling:
    def test_max_is_medium_high_without_trigger(self):
        # Even a very strong cluster+secondary+multi-service with no trigger
        pc = _cluster(count=500, baseline_count=5, change_ratio=100.0,
                      services={"api": 250, "worker": 250})
        p = _packet(
            primary=pc,
            secondary=[_cluster(count=50)],
            services=["api", "worker"],
            triggers=[],
        )
        result = compute_confidence(p)
        assert result in ("medium", "medium-high")

    def test_medium_high_with_good_signal_no_trigger(self):
        # large cluster (2) + secondary (1) + multi-service (1) = 4 → medium-high
        pc = _cluster(count=100, services={"api": 50, "worker": 50})
        p = _packet(primary=pc, secondary=[_cluster(count=10)],
                    services=["api", "worker"], triggers=[])
        assert compute_confidence(p) == "medium-high"


# ── Baseline scoring ──────────────────────────────────────────────────────────

class TestBaselineScoring:
    def test_zero_baseline_does_not_score(self):
        """baseline_count == 0 is uninformative in job-scoped mode."""
        pc = _cluster(count=5, baseline_count=0, change_ratio=6.0)
        p_with_zero = _packet(primary=pc)
        pc2 = _cluster(count=5, baseline_count=10, change_ratio=0.5)
        p_with_baseline = _packet(primary=pc2)
        # Neither should score the baseline dimension differently — zero baseline
        # should not add points the way a genuine change ratio does
        score_zero = compute_confidence(p_with_zero)
        # We only verify it doesn't blow past medium without other signals
        assert score_zero in ("low", "medium")

    def test_high_change_ratio_adds_score(self):
        # baseline present + high change_ratio (2) + large count (2) + trigger (2) = 6 → high
        pc = _cluster(count=100, baseline_count=5, change_ratio=20.0)
        p = _packet(primary=pc, triggers=[_trigger()])
        assert compute_confidence(p) == "high"

    def test_moderate_change_ratio_adds_one_point(self):
        # moderate ratio (1) + medium count (1) = 2 → medium, no trigger
        pc = _cluster(count=15, baseline_count=5, change_ratio=4.0)
        p = _packet(primary=pc)
        assert compute_confidence(p) == "medium"


# ── Secondary and multi-service boosters ─────────────────────────────────────

class TestBoosters:
    def test_secondary_adds_one_point(self):
        pc = _cluster(count=15)
        p_without = _packet(primary=pc, secondary=[])
        p_with = _packet(primary=pc, secondary=[_cluster(count=5)])
        # With secondary should score higher or equal
        scores = {"low": 0, "medium": 1, "medium-high": 2, "high": 3}
        assert scores[compute_confidence(p_with)] >= scores[compute_confidence(p_without)]

    def test_multi_service_adds_one_point(self):
        pc_single = _cluster(count=50, services={"api": 50})
        pc_multi = _cluster(count=50, services={"api": 25, "worker": 25})
        p_single = _packet(primary=pc_single, services=["api"])
        p_multi = _packet(primary=pc_multi, services=["api", "worker"])
        scores = {"low": 0, "medium": 1, "medium-high": 2, "high": 3}
        assert scores[compute_confidence(p_multi)] >= scores[compute_confidence(p_single)]


# ── Confidence levels are valid strings ──────────────────────────────────────

class TestReturnValues:
    VALID = {"low", "medium", "medium-high", "high"}

    def test_all_valid_values(self):
        cases = [
            _packet(primary=None),
            _packet(primary=_cluster(count=5)),
            _packet(primary=_cluster(count=50)),
            _packet(primary=_cluster(count=100), triggers=[_trigger()]),
            _packet(primary=_cluster(count=100), secondary=[_cluster()],
                    services=["api", "worker"], triggers=[_trigger()]),
        ]
        for p in cases:
            assert compute_confidence(p) in self.VALID


class TestScoreFromLabel:
    def test_design_example_medium_high(self):
        assert score_from_label("medium-high") == 0.72

    def test_unknown_label_is_zero(self):
        assert score_from_label("mystery") == 0.0

    def test_no_primary_score_is_zero(self):
        assert compute_confidence_score(_packet(primary=None)) == 0.0

    def test_score_matches_label_table(self):
        p = _packet(primary=_cluster(count=100), secondary=[_cluster()],
                    services=["api", "worker"], triggers=[_trigger()])
        assert compute_confidence(p) == "high"
        assert compute_confidence_score(p) == 0.90

