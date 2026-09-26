"""#209 M2b — every metric-sample reducer groups by series, not by metric name. Pure, no DB.

Ingestion now retains every series of an instrument (per cpu.mode, per route, per replica). The
reducers that already owned metric samples — ranker features, trigger onsets and the structural metric
sig — must reduce per series instead of averaging them into a line that describes none of them, and
samples without recorded identity must behave exactly as before."""
from datetime import datetime, timedelta, timezone

from src.core.rca.features import metric_features
from src.core.rca.observable import State
from src.core.rca.structural_model import summarize_metrics
from src.core.rca.triggers import metric_anomaly_onsets

_W = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_B = _W - timedelta(seconds=300)
_E = _W + timedelta(seconds=300)
_INST = {"service.instance.id": "pod-1"}


class _M:
    def __init__(self, service, metric, value, ts, attributes=None):
        self.service, self.metric, self.value, self.ts, self.attributes = service, metric, value, ts, attributes


def _series(service, metric, base, inc, attributes, n=6):
    out = [_M(service, metric, base, _W - timedelta(seconds=30 * (i + 1)), attributes) for i in range(n)]
    out += [_M(service, metric, inc, _W + timedelta(seconds=30 * (i + 1)), attributes) for i in range(n)]
    return out


def _cpu_modes():
    # saturated CPU split by cpu.mode: idle 0.9 -> 0.1, user 0.1 -> 0.9 (the blended mean stays 0.5)
    return (_series("h", "system.cpu.utilization", 0.9, 0.1, {"cpu.mode": "idle", **_INST})
            + _series("h", "system.cpu.utilization", 0.1, 0.9, {"cpu.mode": "user", **_INST}))


def _strip(samples):
    return [_M(m.service, m.metric, m.value, m.ts, None) for m in samples]


class TestRankerFeatures:
    def test_per_mode_change_is_not_averaged_away(self):
        assert metric_features(_cpu_modes(), _B, _W, _E)["h"] > 1.0  # user series moved 9x

    def test_identity_less_samples_are_one_series_as_before(self):
        # the same values without identity blend exactly like the pre-identity reducer did
        assert metric_features(_strip(_cpu_modes()), _B, _W, _E)["h"] < 1e-6


class TestTriggerOnsets:
    def test_per_mode_onset_is_detected(self):
        onsets = metric_anomaly_onsets(_cpu_modes(), _W, _E)
        assert onsets and onsets[0].service == "h" and onsets[0].metric == "system.cpu.utilization"

    def test_identity_less_samples_are_one_series_as_before(self):
        assert metric_anomaly_onsets(_strip(_cpu_modes()), _W, _E) == []

    def test_one_candidate_per_metric_so_series_cannot_flood_the_budget(self):
        # Eight cpu.mode series all move; a different instrument on another service moves less. With
        # max_candidates=5 the list must hold ONE system.cpu.utilization onset, not five copies of it.
        modes = ("idle", "user", "system", "nice", "iowait", "irq", "softirq", "steal")
        samples = []
        for i, mode in enumerate(modes):
            samples += _series("h", "system.cpu.utilization", 0.1, 0.5 + 0.01 * i, {"cpu.mode": mode, **_INST})
        samples += _series("ad", "jvm.cpu.recent_utilization", 0.2, 0.9, _INST)
        onsets = metric_anomaly_onsets(samples, _W, _E, max_candidates=5)
        keys = [(o.service, o.metric) for o in onsets]
        assert keys.count(("h", "system.cpu.utilization")) == 1
        assert ("ad", "jvm.cpu.recent_utilization") in keys


class TestStructuralMetricSig:
    def _routes(self):
        # route A latency 10ms -> 30ms (3x); route B 100ms -> 100ms. Blended: 55 -> 65 (1.18x, "normal").
        return (_series("api", "latency_ms", 10.0, 30.0, {"http.route": "/a", **_INST})
                + _series("api", "latency_ms", 100.0, 100.0, {"http.route": "/b", **_INST})
                + _series("api", "error_rate", 0.0, 0.0, {"http.route": "/a", **_INST})
                + _series("api", "error_rate", 0.0, 0.0, {"http.route": "/b", **_INST}))

    def test_one_slow_route_is_present_not_averaged_away(self):
        sig = summarize_metrics(self._routes(), _W)["api"]
        assert sig.sig_state == State.PRESENT and abs(sig.latency_ratio - 3.0) < 1e-9

    def test_absent_needs_every_series_measured_and_normal(self):
        normal = (_series("api", "latency_ms", 10.0, 11.0, {"http.route": "/a", **_INST})
                  + _series("api", "error_rate", 0.0, 0.0, {"http.route": "/a", **_INST}))
        assert summarize_metrics(normal, _W)["api"].sig_state == State.ABSENT
        # a second route seen only in the incident has no baseline -> latency not measured everywhere
        extra = [m for m in _series("api", "latency_ms", 10.0, 11.0, {"http.route": "/b", **_INST})
                 if m.ts >= _W]
        assert summarize_metrics(normal + extra, _W)["api"].sig_state is None

    def test_identity_less_samples_are_one_series_as_before(self):
        sig = summarize_metrics(_strip(self._routes()), _W)["api"]
        assert sig.sig_state == State.ABSENT and abs(sig.latency_ratio - 65 / 55) < 1e-9
