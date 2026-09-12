"""Unit tests for multi-modal RCA feature extraction (#118 C1b). No database —
the feature math is pure; :func:`compute_features` (the DB layer) is exercised in
integration."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.core.rca.features import (
    FEATURE_NAMES,
    ServiceFeatures,
    assemble_features,
    log_features,
    metric_features,
    trace_features,
)

INJECT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
INCIDENT_END = INJECT + timedelta(seconds=600)
BASELINE_START = INJECT - timedelta(seconds=300)


def _log(service, level, message, ts, fingerprint="fp"):
    return SimpleNamespace(
        service=service, level=level, raw_message=message, normalized_message=message,
        timestamp=ts, fingerprint=fingerprint,
    )


def _span(service, ts, duration_ms):
    return SimpleNamespace(service=service, start_time=ts, duration_ms=duration_ms)


def _metric(service, metric, value, ts):
    return SimpleNamespace(service=service, metric=metric, value=value, ts=ts)


class TestLogFeatures:
    def test_error_counts_grp_max_and_stack(self):
        entries = [
            _log("cart", "error", "boom", INJECT + timedelta(seconds=1), fingerprint="a"),
            _log("cart", "error", "boom", INJECT + timedelta(seconds=2), fingerprint="a"),
            _log("cart", "error", "other", INJECT + timedelta(seconds=3), fingerprint="b"),
            _log("cart", "info", "fine", INJECT + timedelta(seconds=4)),
            _log("pay", "fatal", "at com.foo.Bar(Bar.java:12)", INJECT + timedelta(seconds=5), fingerprint="c"),
        ]
        err, grp, stack = log_features(entries, INJECT, INCIDENT_END)
        assert err == {"cart": 3, "pay": 1}  # info excluded; fatal counts
        assert grp["cart"] == 2  # largest single (service,fingerprint) group ("a")
        assert grp["pay"] == 1
        assert stack == {"pay": 1}  # only the stack-trace line matched

    def test_out_of_window_dropped(self):
        entries = [
            _log("cart", "error", "x", INJECT - timedelta(seconds=1)),  # before incident
            _log("cart", "error", "x", INCIDENT_END + timedelta(seconds=1)),  # after
            _log("cart", "error", "x", INJECT + timedelta(seconds=10)),  # in
        ]
        err, _grp, _stack = log_features(entries, INJECT, INCIDENT_END)
        assert err == {"cart": 1}


class TestTraceFeatures:
    def test_rate_and_duration_ratios(self):
        spans = (
            # baseline: 1 span for cart (300s window) at 100ms
            [_span("cart", BASELINE_START + timedelta(seconds=10), 100.0)]
            # incident: 4 spans for cart (600s window) at 400ms -> rate & dur up
            + [_span("cart", INJECT + timedelta(seconds=i), 400.0) for i in range(4)]
        )
        rate, dur = trace_features(spans, BASELINE_START, INJECT, INCIDENT_END)
        # rate = (4/600) / (1/300) = 2.0 (within eps)
        assert rate["cart"] == pytest.approx(2.0, rel=1e-3)
        # p95 with <20 points falls back to max: (400+1)/(100+1)
        assert dur["cart"] == pytest.approx(401.0 / 101.0, rel=1e-6)

    def test_only_incident_services_get_entries(self):
        spans = [_span("baseonly", BASELINE_START + timedelta(seconds=1), 10.0)]
        rate, dur = trace_features(spans, BASELINE_START, INJECT, INCIDENT_END)
        assert rate == {} and dur == {}


class TestMetricFeatures:
    def test_max_change_ratio_across_metrics(self):
        samples = [
            # cpu: baseline mean 1.0, incident mean 2.0 -> ratio 1.0
            _metric("cart", "cpu", 1.0, BASELINE_START + timedelta(seconds=1)),
            _metric("cart", "cpu", 2.0, INJECT + timedelta(seconds=1)),
            # mem: baseline mean 10, incident mean 40 -> ratio 3.0 (the max)
            _metric("cart", "mem", 10.0, BASELINE_START + timedelta(seconds=1)),
            _metric("cart", "mem", 40.0, INJECT + timedelta(seconds=1)),
        ]
        anom = metric_features(samples, BASELINE_START, INJECT, INCIDENT_END)
        assert anom["cart"] == pytest.approx(3.0, rel=1e-6)

    def test_requires_both_windows(self):
        # metric present only in the incident window -> no ratio computable
        samples = [_metric("cart", "cpu", 5.0, INJECT + timedelta(seconds=1))]
        assert metric_features(samples, BASELINE_START, INJECT, INCIDENT_END) == {}


class TestAssemble:
    def test_union_candidates_and_presence_flags(self):
        table = assemble_features(
            err={"cart": 3},
            grp={"cart": 2},
            stack={"cart": 1},
            rate={"pay": 2.0},
            dur={"pay": 1.5},
            anom={"db": 4.0},
        )
        assert {s.service for s in table.services} == {"cart", "pay", "db"}
        assert table.has_logs and table.has_traces and table.has_metrics
        by = {s.service: s for s in table.services}
        assert by["cart"].log_err == 3 and by["cart"].tr_rate == 0.0
        assert by["pay"].tr_rate == 2.0 and by["pay"].has_metrics == 1  # case-level flag
        assert by["db"].met_anom == 4.0

    def test_absent_modalities_flagged_off(self):
        table = assemble_features(err={"cart": 1}, grp={"cart": 1}, stack={}, rate={}, dur={}, anom={})
        assert table.has_logs is True
        assert table.has_traces is False and table.has_metrics is False
        assert all(s.has_traces == 0 and s.has_metrics == 0 for s in table.services)


class TestServiceFeatures:
    def test_vector_order_and_as_dict(self):
        s = ServiceFeatures(service="cart", log_err=3, met_anom=4.0, has_logs=1)
        assert s.vector()[FEATURE_NAMES.index("log_err")] == 3.0
        assert s.vector()[FEATURE_NAMES.index("met_anom")] == 4.0
        d = s.as_dict()
        assert d["service"] == "cart" and set(d) == {"service", *FEATURE_NAMES}


class TestAbstentionArms:
    # log_rate_arm / metric_arm take duck-typed rows (no DB); compute_window_anomaly
    # is the DB layer, exercised in integration.
    def test_log_arm_none_when_no_logs(self):
        from src.core.rca.features import log_rate_arm
        assert log_rate_arm([], BASELINE_START, INJECT, INCIDENT_END, tau_log=0.25) is None

    def test_log_arm_zero_when_logs_but_no_errors(self):
        from src.core.rca.features import log_rate_arm
        rows = [_log("cart", "info", "ok", INJECT + timedelta(seconds=1))]
        assert log_rate_arm(rows, BASELINE_START, INJECT, INCIDENT_END, tau_log=0.25) == 0.0

    def test_log_arm_positive_on_novel_errors(self):
        from src.core.rca.features import log_rate_arm
        # errors in the incident window, none in baseline -> elevation -> (0,1)
        rows = [_log("cart", "error", "boom", INJECT + timedelta(seconds=i)) for i in range(20)]
        s = log_rate_arm(rows, BASELINE_START, INJECT, INCIDENT_END, tau_log=0.25)
        assert 0.0 < s < 1.0

    def test_log_arm_clamps_when_incident_quieter(self):
        from src.core.rca.features import log_rate_arm
        rows = [_log("cart", "error", "x", BASELINE_START + timedelta(seconds=i)) for i in range(20)]
        # errors only in baseline, none in incident -> no elevation -> 0
        assert log_rate_arm(rows, BASELINE_START, INJECT, INCIDENT_END, tau_log=0.25) == 0.0

    def test_metric_arm_none_when_no_metrics(self):
        from src.core.rca.features import metric_arm
        assert metric_arm([], BASELINE_START, INJECT, INCIDENT_END, tau_metric=4.0) is None

    def test_metric_arm_positive_on_change(self):
        from src.core.rca.features import metric_arm
        samples = [
            _metric("cart", "cpu", 1.0, BASELINE_START + timedelta(seconds=1)),
            _metric("cart", "cpu", 5.0, INJECT + timedelta(seconds=1)),
        ]
        assert metric_arm(samples, BASELINE_START, INJECT, INCIDENT_END, tau_metric=4.0) > 0.0
