"""#209 M2b — utilization observables util:{service}:{resource}. Pure, no DB.

Pins the semantics: a metric is measured only when it is ONE verified series — every sample carries
series identity (datapoint attributes + reporting instance), all samples belong to exactly one series,
no two share a timestamp, and the declared instrument (metric_type) is the one the reducer needs.
Anything else is UNKNOWN, never a measured ABSENT: in particular a per-state gauge
(system.cpu.utilization by cpu.mode) is never averaged into "normal"."""
from datetime import datetime, timedelta, timezone

from src.core.rca.observable import State
from src.core.rca.outcome import resolve
from src.core.rca.partition import partition
from src.core.rca.structural_model import (
    ServiceSignal,
    UtilSignal,
    build_hypotheses,
    build_observables,
    structural_signals,
    summarize_utilization,
    util_metric_class,
)

_W = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_ONE = {"service.instance.id": "pod-1"}  # series identity of an attribute-free instrument


class _M:
    def __init__(self, service, metric, value, ts, *, attributes=None, metric_type=None):
        self.service, self.metric, self.value, self.ts = service, metric, value, ts
        self.attributes, self.metric_type = attributes, metric_type


def _gauge(service, metric, base, inc, n=5, *, attributes=_ONE, offset=0):
    kw = {"attributes": attributes, "metric_type": "gauge"}
    out = [_M(service, metric, base, _W - timedelta(seconds=10 * (i + 1) + offset), **kw) for i in range(n)]
    out += [_M(service, metric, inc, _W + timedelta(seconds=10 * (i + 1) + offset), **kw) for i in range(n)]
    return out


def _counter(service, metric, base_rate, inc_rate, n=5, *, attributes=_ONE, metric_type="counter", offset=0):
    out, v = [], 0.0
    for i in range(n, 0, -1):
        out.append(_M(service, metric, v, _W - timedelta(seconds=10 * i - offset),
                      attributes=attributes, metric_type=metric_type))
        v += base_rate * 10
    for i in range(1, n + 1):
        out.append(_M(service, metric, v, _W + timedelta(seconds=10 * i + offset),
                      attributes=attributes, metric_type=metric_type))
        v += inc_rate * 10
    return out


def _state(samples, key):
    u = summarize_utilization(samples, _W).get(key)
    return u.sig_state if u else "no-signal"


class TestMetricClass:
    def test_name_shape_gives_resource_and_required_instrument(self):
        assert util_metric_class("jvm.cpu.recent_utilization") == ("cpu", "gauge")
        assert util_metric_class("nodejs.eventloop.utilization") == ("eventloop", "gauge")
        assert util_metric_class("system.memory.utilization") == ("memory", "gauge")
        assert util_metric_class("process.cpu.time") == ("cpu", "counter")

    def test_self_telemetry_and_other_metrics_are_not_classified(self):
        assert util_metric_class("otel.sdk.span.started") is None
        assert util_metric_class("otelcol_scraper_scraped_metric_points") is None
        assert util_metric_class("process.memory.usage") is None
        assert util_metric_class(None) is None


class TestOneVerifiedSeriesGauge:
    def test_saturation_is_present(self):
        assert _state(_gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9), ("ad", "cpu")) == State.PRESENT

    def test_normal_is_absent_and_a_drop_is_not_an_anomaly(self):
        assert _state(_gauge("ad", "jvm.cpu.recent_utilization", 0.2, 0.25), ("ad", "cpu")) == State.ABSENT
        assert _state(_gauge("fe", "nodejs.eventloop.utilization", 0.4, 0.1), ("fe", "eventloop")) == State.ABSENT

    def test_per_mode_gauge_is_unknown_not_averaged_into_absent(self):
        # The reviewer's case: cpu.mode idle 0.9->0.1 and user 0.1->0.9. Averaged, both windows read 0.5
        # and the saturated CPU would be "measured normal". Two series -> UNKNOWN.
        samples = (_gauge("h", "system.cpu.utilization", 0.9, 0.1, attributes={"cpu.mode": "idle", **_ONE})
                   + _gauge("h", "system.cpu.utilization", 0.1, 0.9, attributes={"cpu.mode": "user", **_ONE}))
        assert _state(samples, ("h", "cpu")) is None
        assert build_observables({}, None, summarize_utilization(samples, _W)) == []

    def test_per_state_memory_gauge_is_unknown(self):
        samples = (_gauge("h", "system.memory.utilization", 0.3, 0.9, attributes={"system.memory.state": "used"})
                   + _gauge("h", "system.memory.utilization", 0.7, 0.1, attributes={"system.memory.state": "free"}))
        assert _state(samples, ("h", "memory")) is None

    def test_two_reporting_instances_are_unknown(self):
        samples = (_gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9, attributes={"service.instance.id": "a"})
                   + _gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.02,
                            attributes={"service.instance.id": "b"}, offset=3))
        assert _state(samples, ("ad", "cpu")) is None

    def test_samples_without_series_identity_are_never_measured(self):
        assert _state(_gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9, attributes=None), ("ad", "cpu")) is None

    def test_same_timestamp_collision_within_a_series_is_unknown(self):
        samples = _gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9)
        samples.append(_M("ad", "jvm.cpu.recent_utilization", 0.02, samples[0].ts,
                          attributes=_ONE, metric_type="gauge"))
        assert _state(samples, ("ad", "cpu")) is None

    def test_declared_non_gauge_utilization_is_unknown(self):
        samples = _gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9)
        for s in samples:
            s.metric_type = "sum"
        assert _state(samples, ("ad", "cpu")) is None

    def test_malformed_or_zero_baseline_is_unknown(self):
        bad = _gauge("ad", "jvm.cpu.recent_utilization", 0.1, 0.12)
        bad.append(_M("ad", "jvm.cpu.recent_utilization", -1.0, _W + timedelta(seconds=99),
                      attributes=_ONE, metric_type="gauge"))
        assert _state(bad, ("ad", "cpu")) is None
        assert _state(_gauge("ad", "jvm.cpu.recent_utilization", 0.0, 0.9), ("ad", "cpu")) is None

    def test_unattributed_and_self_telemetry_samples_are_not_evidence(self):
        assert summarize_utilization(_gauge(None, "container.cpu.utilization", 0.01, 0.9), _W) == {}
        assert summarize_utilization(_gauge("pc", "otel.sdk.span.started", 10, 20), _W) == {}


class TestOneVerifiedSeriesCounter:
    def test_declared_cumulative_counter_rate_increase_is_present(self):
        assert _state(_counter("ad", "jvm.cpu.time", 0.01, 0.5), ("ad", "cpu")) == State.PRESENT

    def test_steady_rate_is_absent(self):
        assert _state(_counter("ad", "jvm.cpu.time", 0.1, 0.11), ("ad", "cpu")) == State.ABSENT

    def test_name_shape_alone_does_not_make_a_counter(self):
        # a *.cpu.time not declared a cumulative counter (null = gauge, or a delta "sum") is not rated.
        assert _state(_counter("ad", "jvm.cpu.time", 0.01, 0.5, metric_type=None), ("ad", "cpu")) is None
        assert _state(_counter("ad", "jvm.cpu.time", 0.01, 0.5, metric_type="sum"), ("ad", "cpu")) is None

    def test_offset_interleaved_series_with_monotonic_blend_is_unknown(self):
        # The reviewer's case: two cumulative series on offset timestamps whose merged sequence happens to
        # be non-decreasing. With identity they are two series -> UNKNOWN, not a blended rate.
        samples = (_counter("r", "process.cpu.time", 0.1, 0.5, attributes={"cpu.mode": "user", **_ONE})
                   + _counter("r", "process.cpu.time", 0.1, 0.5, attributes={"cpu.mode": "system", **_ONE},
                              offset=5))
        assert _state(samples, ("r", "cpu")) is None

    def test_reset_is_unknown(self):
        samples = _counter("ad", "jvm.cpu.time", 0.1, 0.5)
        samples.append(_M("ad", "jvm.cpu.time", 0.0, _W + timedelta(seconds=61),
                          attributes=_ONE, metric_type="counter"))
        assert _state(samples, ("ad", "cpu")) is None


class TestWitnessAcrossMetricsOfAClass:
    def test_present_metric_wins_the_class(self):
        samples = _gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9) + _counter("ad", "jvm.cpu.time", 0.1, 0.11)
        u = summarize_utilization(samples, _W)[("ad", "cpu")]
        assert u.sig_state == State.PRESENT and u.ratio > 2.0


def _util(service, resource, state):
    return UtilSignal(service, resource, 5.0 if state == State.PRESENT else 1.0, True, state)


class TestModel:
    def test_present_util_generates_its_service_with_a_util_expectation(self):
        (h,) = build_hypotheses({}, set(), None, {("ad", "cpu"): _util("ad", "cpu", State.PRESENT)})
        assert h.localization == "ad"
        assert h.predictions == {"sig:ad": State.PRESENT, "util:ad:cpu": State.PRESENT}

    def test_absent_util_does_not_generate(self):
        assert build_hypotheses({}, set(), None, {("ad", "cpu"): _util("ad", "cpu", State.ABSENT)}) == []

    def test_without_util_signals_the_m2a_model_is_unchanged(self):
        signals = {"checkout": ServiceSignal("checkout", 0.5, 1.0, True, True, State.PRESENT)}
        (h,) = build_hypotheses(signals, set(), {})
        assert h.predictions == {"sig:checkout": State.PRESENT}

    def test_util_observable_is_its_own_coordinate(self):
        obs = build_observables({"ad": ServiceSignal("ad", 0.0, 1.0, False, False, None)}, None,
                                {("ad", "cpu"): _util("ad", "cpu", State.PRESENT)})
        assert [o.id for o in obs] == ["util:ad:cpu"]


class TestLocallySilentResourceFaultEndToEnd:
    def _metrics(self):
        return (_gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9)
                + _gauge("cart", "jvm.cpu.recent_utilization", 0.2, 0.2))

    def test_m2b_retains_the_saturated_service(self):
        inp = structural_signals([], self._metrics(), _W)
        res = resolve(partition(
            build_hypotheses(inp.signals, inp.edges, inp.edge_signals, inp.util_signals),
            build_observables(inp.signals, inp.edge_signals, inp.util_signals)))
        assert res.localization == ("ad",)

    def test_without_series_identity_nothing_is_claimed(self):
        # the same evidence from a source that never recorded series identity: no util observable at all.
        stripped = [_M(m.service, m.metric, m.value, m.ts, metric_type="gauge") for m in self._metrics()]
        inp = structural_signals([], stripped, _W)
        assert inp.util_signals[("ad", "cpu")].sig_state is None
        assert build_hypotheses(inp.signals, inp.edges, inp.edge_signals, inp.util_signals) == []
