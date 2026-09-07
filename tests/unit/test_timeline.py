from datetime import datetime, timedelta, timezone

from src.core.clustering.clusterer import ClusterData
from src.core.explain.evidence import EvidencePacket
from src.core.timeline.builder import build_timeline

BASE = datetime(2026, 1, 1, 22, 0, 0, tzinfo=timezone.utc)


def _cluster(message: str, offset_minutes: int, fingerprint: str = "") -> ClusterData:
    ts = BASE + timedelta(minutes=offset_minutes)
    return ClusterData(
        fingerprint=fingerprint or message,
        representative_message=message,
        count=10,
        services={"api": 10},
        levels={"error": 10},
        first_seen=ts,
        last_seen=ts,
        baseline_count=0,
        change_ratio=0.0,
        importance_score=1.0,
    )


def _packet(primary: ClusterData, secondary: list[ClusterData]) -> EvidencePacket:
    return EvidencePacket(
        window_start=BASE,
        window_end=BASE + timedelta(hours=1),
        total_logs=100,
        primary_cluster=primary,
        secondary_clusters=secondary,
        trigger_candidates=[],
        evidence_items=[],
        services_affected=["api"],
    )


class TestTimelineOrdering:
    def test_error_effect_sorts_before_earlier_symptom(self):
        """Regression for #65: severity ranks before timestamp.

        A 500/error effect must sort ahead of a queue-backlog symptom even
        when the symptom appeared first — the docstring promises "error
        effects before … symptoms last", not chronological order.
        """
        primary = _cluster("primary 500 error", 0)
        symptom = _cluster("queue backlog growing", 1)   # earlier
        effect = _cluster("checkout 500 error failed", 5)  # later, more severe

        timeline = build_timeline(_packet(primary, [symptom, effect]))
        categories = [e.category for e in timeline]

        # primary error first, then the effect, then the symptom last
        assert categories == ["error", "effect", "symptom"]
        # the later-but-more-severe effect precedes the earlier symptom
        eff_idx = next(i for i, e in enumerate(timeline) if "checkout" in e.description)
        sym_idx = next(i for i, e in enumerate(timeline) if "backlog" in e.description)
        assert eff_idx < sym_idx

    def test_timestamp_breaks_ties_within_category(self):
        """Equally-severe effects still order by timestamp."""
        primary = _cluster("primary 500 error", 0)
        later = _cluster("checkout 500 error late", 9)
        earlier = _cluster("payment 500 error early", 3)

        timeline = build_timeline(_packet(primary, [later, earlier]))
        effects = [e.description for e in timeline if e.category == "effect"]
        assert effects.index("payment 500 error early") < effects.index("checkout 500 error late")
