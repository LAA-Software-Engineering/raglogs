"""Unit tests for the hierarchical metric anomaly detector (#79 Gen-3.1). No DB.

Covers the two failures the OTel validation exposed:
- near-zero-baseline counters must NOT explode (bounded, log-space effect size);
- a lone twitch among many metrics must NOT flag a service (corroboration): a
  service is anomalous only when >=k of its metrics agree.
"""
from datetime import datetime, timedelta, timezone

from src.core.rca.metric_semantics import metric_anomaly_by_type

INJECT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
INCIDENT_END = INJECT + timedelta(seconds=300)
BASELINE_START = INJECT - timedelta(seconds=300)


def _m(service, metric, value, ts, metric_type=None):
    from types import SimpleNamespace
    return SimpleNamespace(service=service, metric=metric, value=value, ts=ts, metric_type=metric_type)


def _counter(service, metric, start, n, step_s, v0, dv):
    return [_m(service, metric, v0 + i * dv, start + timedelta(seconds=i * step_s), "counter")
            for i in range(n)]


def _run(samples, **kw):
    return metric_anomaly_by_type(samples, BASELINE_START, INJECT, INCIDENT_END,
                                  tau=kw.pop("tau", 1.0), **kw)


class TestNearZeroBaselineBounded:
    def test_near_zero_baseline_counter_is_bounded_not_exploded(self):
        # 3 counters each ~0 in baseline then active in incident. Pre-fix this gave
        # rate_inc/eps ~ 1e9; now each per-metric anomaly is a saturated log-space
        # effect in [0,1), so the corroborated service score is bounded < 1.
        s = []
        for j in range(3):
            s += _counter("cart", f"c{j}", BASELINE_START, 4, 60, v0=100, dv=0)   # flat -> rate 0
            s += _counter("cart", f"c{j}", INJECT, 4, 60, v0=100, dv=480)          # ~8/s
        out = _run(s, k=3, corroboration_threshold=0.5)
        assert "cart" in out and 0.0 < out["cart"] < 1.0  # bounded, not 1e9


class TestCorroboration:
    def test_lone_twitch_does_not_flag_service(self):
        # one counter spikes; four others steady. count above T = 1 < k=3 -> omitted.
        s = _counter("cart", "spike", BASELINE_START, 4, 60, 0, 0) + \
            _counter("cart", "spike", INJECT, 4, 60, 0, 900)  # big jump
        for j in range(4):  # quiet, steady counters
            s += _counter("cart", f"q{j}", BASELINE_START, 4, 60, 0, 300) + \
                 _counter("cart", f"q{j}", INJECT, 4, 60, 1200, 300)  # same 5/s rate
        assert "cart" not in _run(s, k=3, corroboration_threshold=0.5)  # single twitch ignored

    def test_several_agreeing_metrics_flag_service(self):
        # 3 counters all jump from ~0 to a high rate -> corroborated -> scored high
        s = []
        for j in range(3):
            s += _counter("cart", f"m{j}", BASELINE_START, 4, 60, 0, 60) + \
                 _counter("cart", f"m{j}", INJECT, 4, 60, 5000, 6000)  # ~100/s vs 1/s
        out = _run(s, k=3, corroboration_threshold=0.5)
        assert out["cart"] > 0.8


class TestHealthy:
    def test_steady_counters_score_low_and_omitted(self):
        # many steady counters (equal rate both windows) -> per-metric ~0 -> below T
        s = []
        for j in range(6):
            s += _counter("cart", f"m{j}", BASELINE_START, 6, 50, 0, 500) + \
                 _counter("cart", f"m{j}", INJECT, 6, 50, 3000, 500)  # same 10/s
        assert _run(s, k=3, corroboration_threshold=0.5) == {}  # healthy -> nothing


class TestGauge:
    def test_gauge_level_logspace_bounded(self):
        # 3 gauges jump level 1 -> 30; log-space effect |log1p(30)-log1p(1)| bounded
        s = []
        for j in range(3):
            s += [_m("db", f"g{j}", 1.0, BASELINE_START + timedelta(seconds=t), "gauge") for t in (1, 2)]
            s += [_m("db", f"g{j}", 30.0, INJECT + timedelta(seconds=t), "gauge") for t in (1, 2)]
        out = _run(s, k=3, corroboration_threshold=0.3)
        assert "db" in out and 0.0 < out["db"] < 1.0

    def test_untyped_treated_as_level(self):
        s = []
        for j in range(3):
            s += [_m("s", f"m{j}", 10.0, BASELINE_START + timedelta(seconds=1)),
                  _m("s", f"m{j}", 40.0, INJECT + timedelta(seconds=1))]
        assert "s" in _run(s, k=3, corroboration_threshold=0.3)


class TestResetRobust:
    def test_counter_reset_within_window_not_spurious(self):
        # 3 counters, each resets mid-incident at the baseline rate -> ~0, omitted
        s = []
        for j in range(3):
            s += _counter("cart", f"m{j}", BASELINE_START, 6, 50, 1000, 150)  # +750 total
            vals = [4000, 4250, 4500, 0, 250]  # positive deltas 250+250+0+250 = 750
            s += [_m("cart", f"m{j}", v, INJECT + timedelta(seconds=i * 50), "counter")
                  for i, v in enumerate(vals)]
        assert _run(s, k=3, corroboration_threshold=0.5) == {}  # reset handled -> no anomaly
