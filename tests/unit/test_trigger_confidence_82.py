"""Unit tests for the #82 confidence-gate change: a *found* trigger earns points,
but only a *validated* (explains) trigger gates "high" — and the legacy path
(flags None) stays byte-identical. No DB."""
from datetime import datetime, timezone

from src.core.clustering.clusterer import ClusterData
from src.core.explain.confidence import compute_confidence
from src.core.explain.evidence import EvidencePacket, TriggerCandidate, _primary_service

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _primary():
    # Scores 4 points without any trigger: count_high(+2) + change_high(+2).
    return ClusterData(
        fingerprint="fp", representative_message="boom", count=100,
        services={"cart": 100, "web": 10}, levels={"error": 100},
        first_seen=T0, last_seen=T0, baseline_count=5, change_ratio=20.0,
        importance_score=1.0, error_service_counts={"cart": 100},
    )


def _packet(**kw) -> EvidencePacket:
    base = dict(
        window_start=T0, window_end=T0, total_logs=100, primary_cluster=_primary(),
        secondary_clusters=[_primary()],  # +1 secondary
        trigger_candidates=[], evidence_items=[], services_affected=["cart", "web"],  # +1 multiservice
    )
    base.update(kw)
    return EvidencePacket(**base)


class TestRareEventModeNeverClaimsHigh:
    # T3 (#82): a rare+linked trigger fires on nearly every log-announced incident,
    # so rare_event mode must not ride confidence on it. In that mode the label is
    # evidence-volume-only, capped at "medium-high" — no "high" claim until
    # confidence is calibrated (#83 / Phase D).
    def test_found_but_unlinked_trigger_caps_at_medium_high(self):
        pkt = _packet(
            trigger_candidates=[TriggerCandidate("deploy Y", T0, "unrelated")],
            trigger_found=True, trigger_explains=False,
        )
        assert compute_confidence(pkt) == "medium-high"

    def test_validated_trigger_still_capped_at_medium_high(self):
        # even a rare + linked ("explains") trigger no longer reaches "high"
        pkt = _packet(
            trigger_candidates=[TriggerCandidate("deploy cart", T0, "cart")],
            trigger_found=True, trigger_explains=True,
        )
        assert compute_confidence(pkt) == "medium-high"

    def test_trigger_adds_no_points_in_rare_event_mode(self):
        # confidence is identical whether or not a rare-event trigger was found
        from src.core.explain.confidence import compute_confidence_points

        with_trigger = _packet(
            trigger_candidates=[TriggerCandidate("x", T0, "cart")],
            trigger_found=True, trigger_explains=True,
        )
        without = _packet(trigger_found=False, trigger_explains=False)
        assert compute_confidence_points(with_trigger) == compute_confidence_points(without)


class TestLegacyBackCompat:
    def test_none_flags_with_candidates_behave_as_before(self):
        # flags unset (regex mode / direct construction) + a candidate present ->
        # old behaviour: reaches "high".
        pkt = _packet(trigger_candidates=[TriggerCandidate("token expired", T0, "cart")])
        assert pkt.trigger_found is None and pkt.trigger_explains is None
        assert compute_confidence(pkt) == "high"

    def test_none_flags_without_candidates_behave_as_before(self):
        pkt = _packet(trigger_candidates=[])
        assert compute_confidence(pkt) == "medium-high"


class TestPrimaryService:
    def test_prefers_error_service_then_volume(self):
        assert _primary_service(_primary()) == "cart"

    def test_none_primary(self):
        assert _primary_service(None) is None
