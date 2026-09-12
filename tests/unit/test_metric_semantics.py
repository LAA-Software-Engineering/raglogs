"""Unit tests for metric-type-aware anomaly normalization (#79 Gen-3). No DB.

The headline case: a HEALTHY cumulative counter that keeps increasing must score
~0 (not saturate), which is the OTel failure the fresh-corpus validation exposed."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.core.rca.metric_semantics import metric_anomaly_by_type

INJECT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
INCIDENT_END = INJECT + timedelta(seconds=300)
BASELINE_START = INJECT - timedelta(seconds=300)


def _m(service, metric, value, ts, metric_type=None):
    return SimpleNamespace(service=service, metric=metric, value=value, ts=ts, metric_type=metric_type)


def _series(service, metric, mtype, start, n, step_s, v0, dv):
    """n samples every step_s seconds, value v0 + i*dv (linear)."""
    return [_m(service, metric, v0 + i * dv, start + timedelta(seconds=i * step_s), mtype)
            for i in range(n)]


class TestCounter:
    def test_healthy_increasing_counter_scores_near_zero(self):
        # cumulative counter climbing at a STEADY rate across baseline and incident
        # windows — the raw mean is very different, but the rate is identical → ~0.
        base = _series("cart", "reqs", "counter", BASELINE_START, 6, 50, v0=1000, dv=500)
        inc = _series("cart", "reqs", "counter", INJECT, 6, 50, v0=4000, dv=500)  # same 10/s slope
        a = metric_anomaly_by_type(base + inc, BASELINE_START, INJECT, INCIDENT_END)
        assert a["cart"] == pytest.approx(0.0, abs=1e-6)  # steady counter → no anomaly

    def test_accelerating_counter_scores_high(self):
        # incident window's counter climbs much faster → large rate elevation
        base = _series("cart", "reqs", "counter", BASELINE_START, 6, 50, v0=1000, dv=100)   # 2/s
        inc = _series("cart", "reqs", "counter", INJECT, 6, 50, v0=1500, dv=2000)           # 40/s
        a = metric_anomaly_by_type(base + inc, BASELINE_START, INJECT, INCIDENT_END)
        assert a["cart"] > 5.0  # rate jumped ~20x

    def test_single_point_window_no_rate(self):
        # a counter needs >=2 points per window to define a rate
        s = [_m("cart", "reqs", 1000, BASELINE_START, "counter"),
             _m("cart", "reqs", 9999, INJECT + timedelta(seconds=5), "counter")]
        assert metric_anomaly_by_type(s, BASELINE_START, INJECT, INCIDENT_END) == {}


class TestGaugeUnchanged:
    def test_gauge_level_relative_change(self):
        # gauge path is the original behavior: |mean_inc - mean_base|/|mean_base|
        base = [_m("db", "cpu", 1.0, BASELINE_START + timedelta(seconds=1), "gauge"),
                _m("db", "cpu", 1.0, BASELINE_START + timedelta(seconds=2), "gauge")]
        inc = [_m("db", "cpu", 3.0, INJECT + timedelta(seconds=1), "gauge"),
               _m("db", "cpu", 3.0, INJECT + timedelta(seconds=2), "gauge")]
        a = metric_anomaly_by_type(base + inc, BASELINE_START, INJECT, INCIDENT_END)
        assert a["db"] == pytest.approx(2.0, rel=1e-6)  # (3-1)/1

    def test_untyped_treated_as_gauge(self):
        # metric_type=None (e.g. RCAEval melted columns) → gauge level comparison
        base = [_m("s", "m", 10.0, BASELINE_START + timedelta(seconds=1)),
                _m("s", "m", 10.0, BASELINE_START + timedelta(seconds=2))]
        inc = [_m("s", "m", 40.0, INJECT + timedelta(seconds=1)),
               _m("s", "m", 40.0, INJECT + timedelta(seconds=2))]
        a = metric_anomaly_by_type(base + inc, BASELINE_START, INJECT, INCIDENT_END)
        assert a["s"] == pytest.approx(3.0, rel=1e-6)  # (40-10)/10


class TestAggregation:
    def test_per_service_max_over_metrics(self):
        quiet = _series("cart", "reqs", "counter", BASELINE_START, 4, 60, 0, 60) + \
                _series("cart", "reqs", "counter", INJECT, 4, 60, 240, 60)  # steady 1/s → 0
        loud = [_m("cart", "cpu", 1.0, BASELINE_START + timedelta(seconds=1), "gauge"),
                _m("cart", "cpu", 1.0, BASELINE_START + timedelta(seconds=2), "gauge"),
                _m("cart", "cpu", 5.0, INJECT + timedelta(seconds=1), "gauge"),
                _m("cart", "cpu", 5.0, INJECT + timedelta(seconds=2), "gauge")]
        a = metric_anomaly_by_type(quiet + loud, BASELINE_START, INJECT, INCIDENT_END)
        assert a["cart"] == pytest.approx(4.0, rel=1e-6)  # max(counter~0, gauge 4.0)

    def test_requires_both_windows(self):
        # metric only in incident window → no baseline → excluded
        inc = [_m("s", "m", 1.0, INJECT + timedelta(seconds=1), "gauge")]
        assert metric_anomaly_by_type(inc, BASELINE_START, INJECT, INCIDENT_END) == {}
