"""Unit tests for rare-event trigger candidate extraction (#82 T1). No DB."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.core.rca.triggers import rare_event_candidates

ONSET = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class FakeCluster:
    fingerprint: str
    representative_message: str
    services: dict
    baseline_count: int
    change_ratio: float
    first_seen: Optional[datetime]
    levels: dict = field(default_factory=dict)


def _c(fp, msg, svc, baseline_count, change_ratio, first_seen):
    return FakeCluster(fp, msg, {svc: 1}, baseline_count, change_ratio, first_seen)


class TestRarityFilter:
    def test_only_rare_clusters_qualify(self):
        clusters = [
            _c("a", "deploy of cart v2", "cart", 0, 1.0, ONSET - timedelta(seconds=30)),   # never seen -> rare
            _c("b", "usual heartbeat", "cart", 500, 1.1, ONSET - timedelta(seconds=30)),   # common -> not rare
            _c("d", "spike", "cart", 2, 12.0, ONSET - timedelta(seconds=30)),              # high change_ratio -> rare
        ]
        got = {t.fingerprint for t in rare_event_candidates(clusters, ONSET, rare_change_ratio=5.0)}
        assert got == {"a", "d"}

    def test_candidate_after_onset_excluded(self):
        clusters = [
            _c("late", "x", "cart", 0, 1.0, ONSET + timedelta(seconds=300)),  # well after onset
            _c("early", "y", "cart", 0, 1.0, ONSET - timedelta(seconds=10)),
        ]
        got = [t.fingerprint for t in rare_event_candidates(clusters, ONSET)]
        assert got == ["early"]

    def test_grace_allows_near_concurrent(self):
        clusters = [_c("g", "x", "cart", 0, 1.0, ONSET + timedelta(seconds=60))]
        assert rare_event_candidates(clusters, ONSET, onset_grace_seconds=120)  # within grace -> kept


class TestRanking:
    def test_earlier_and_rarer_scores_higher(self):
        clusters = [
            _c("early", "deploy", "cart", 0, 1.0, ONSET - timedelta(seconds=300)),
            _c("later", "deploy", "cart", 0, 1.0, ONSET - timedelta(seconds=5)),
        ]
        ranked = rare_event_candidates(clusters, ONSET)
        assert [t.fingerprint for t in ranked] == ["early", "later"]  # earlier lead -> higher score

    def test_max_candidates_caps(self):
        clusters = [_c(str(i), "m", "s", 0, 1.0, ONSET - timedelta(seconds=i + 1)) for i in range(10)]
        assert len(rare_event_candidates(clusters, ONSET, max_candidates=3)) == 3

    def test_type_inferred_and_dominant_service(self):
        c = FakeCluster("a", "Deployment rolled out", {"cart": 5, "web": 1}, 0, 1.0, ONSET - timedelta(seconds=10))
        t = rare_event_candidates([c], ONSET)[0]
        assert t.service == "cart"  # dominant by volume
        assert t.trigger_type == "deploy"  # regex demoted to a type label
        assert t.baseline_count == 0

    def test_no_onset_ranks_by_rarity(self):
        clusters = [
            _c("rare", "x", "s", 0, 1.0, None),
            _c("less", "y", "s", 3, 8.0, None),
        ]
        ranked = rare_event_candidates(clusters, None, rare_change_ratio=5.0)
        assert ranked[0].fingerprint == "rare"  # baseline_count 0 -> rarity 1.0 beats 1/4

    def test_tiebreak_mixed_naive_aware_first_seen(self):
        # Equal score (both never-seen, no onset) -> tiebreak on first_seen.
        # A naive timestamp is treated as UTC, so it must order against an aware
        # one by wall-clock without raising on naive/aware comparison.
        naive_earlier = datetime(2026, 1, 1, 11, 0, 0)  # 11:00, naive == UTC
        aware_later = datetime(2026, 1, 1, 11, 30, 0, tzinfo=timezone.utc)
        clusters = [
            _c("aware", "x", "s", 0, 1.0, aware_later),
            _c("naive", "y", "s", 0, 1.0, naive_earlier),
        ]
        ranked = rare_event_candidates(clusters, None)
        assert [t.fingerprint for t in ranked] == ["naive", "aware"]  # earlier wall-clock first


class TestMetricAnomalyOnsets:
    from datetime import datetime as _dt, timezone as _tz
    I0 = _dt(2026, 1, 1, 12, 0, 0, tzinfo=_tz.utc)

    def _s(self, service, metric, value, ts):
        from types import SimpleNamespace
        return SimpleNamespace(service=service, metric=metric, value=value, ts=ts)

    def test_detects_earliest_deviation_after_flat_baseline(self):
        from datetime import timedelta
        from src.core.rca.triggers import metric_anomaly_onsets
        I0 = self.I0
        samples = (
            # flat baseline mean 1.0 before incident
            [self._s("cart", "cpu", 1.0, I0 - timedelta(seconds=s)) for s in (40, 30, 20, 10)]
            # incident: normal, normal, then a spike at +120s
            + [self._s("cart", "cpu", 1.0, I0 + timedelta(seconds=30)),
               self._s("cart", "cpu", 1.05, I0 + timedelta(seconds=60)),
               self._s("cart", "cpu", 9.0, I0 + timedelta(seconds=120))]
        )
        onsets = metric_anomaly_onsets(samples, I0, I0 + timedelta(seconds=600))
        assert len(onsets) == 1
        assert onsets[0].service == "cart" and onsets[0].metric == "cpu"
        assert onsets[0].onset == I0 + timedelta(seconds=120)  # the spike, not the earlier normal points

    def test_needs_enough_baseline(self):
        from datetime import timedelta
        from src.core.rca.triggers import metric_anomaly_onsets
        I0 = self.I0
        samples = [self._s("cart", "cpu", 1.0, I0 - timedelta(seconds=10)),  # only 1 baseline point
                   self._s("cart", "cpu", 9.0, I0 + timedelta(seconds=30))]
        assert metric_anomaly_onsets(samples, I0, I0 + timedelta(seconds=600), min_baseline=3) == []

    def test_stable_metric_yields_no_onset(self):
        from datetime import timedelta
        from src.core.rca.triggers import metric_anomaly_onsets
        I0 = self.I0
        samples = ([self._s("cart", "cpu", 1.0, I0 - timedelta(seconds=s)) for s in (40, 30, 20, 10)]
                   + [self._s("cart", "cpu", 1.0, I0 + timedelta(seconds=s)) for s in (30, 60, 120)])
        assert metric_anomaly_onsets(samples, I0, I0 + timedelta(seconds=600)) == []
