"""#209 M2b — utilization observables util:{service}:{resource}. Pure, no DB.

Pins the frozen semantics: metrics are classified by OpenTelemetry semantic-convention name shape (not
by names picked from a corpus), collector/SDK self-telemetry is excluded, a cumulative CPU-time rate is
only trusted for a verifiably single-series monotonic stream, unattributed or malformed samples never
become evidence, and the observable family stays separate from sig:{service}."""
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


class _M:
    def __init__(self, service, metric, value, ts):
        self.service, self.metric, self.value, self.ts = service, metric, value, ts


def _gauge(service, metric, base, inc, n=5):
    out = [_M(service, metric, base, _W - timedelta(seconds=10 * (i + 1))) for i in range(n)]
    out += [_M(service, metric, inc, _W + timedelta(seconds=10 * (i + 1))) for i in range(n)]
    return out


def _counter(service, metric, base_rate, inc_rate, n=5):
    """A single monotonic cumulative series: `base_rate`/s before the window, `inc_rate`/s after."""
    out, v = [], 0.0
    for i in range(n, 0, -1):
        out.append(_M(service, metric, v, _W - timedelta(seconds=10 * i)))
        v += base_rate * 10
    for i in range(1, n + 1):
        out.append(_M(service, metric, v, _W + timedelta(seconds=10 * i)))
        v += inc_rate * 10
    return out


class TestMetricClass:
    def test_semantic_convention_shapes(self):
        assert util_metric_class("jvm.cpu.recent_utilization") == ("cpu", "gauge")
        assert util_metric_class("system.cpu.utilization") == ("cpu", "gauge")
        assert util_metric_class("nodejs.eventloop.utilization") == ("eventloop", "gauge")
        assert util_metric_class("system.memory.utilization") == ("memory", "gauge")
        assert util_metric_class("process.cpu.time") == ("cpu", "cpu_time")

    def test_self_telemetry_and_other_metrics_are_not_classified(self):
        assert util_metric_class("otel.sdk.span.started") is None
        assert util_metric_class("otelcol_scraper_scraped_metric_points") is None
        assert util_metric_class("process.memory.usage") is None
        assert util_metric_class(None) is None


class TestGauges:
    def test_saturation_is_present(self):
        u = summarize_utilization(_gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9), _W)[("ad", "cpu")]
        assert u.sig_state == State.PRESENT and u.id == "util:ad:cpu"

    def test_normal_is_absent_and_a_drop_is_not_an_anomaly(self):
        assert summarize_utilization(_gauge("ad", "jvm.cpu.recent_utilization", 0.2, 0.25), _W)[
            ("ad", "cpu")].sig_state == State.ABSENT
        assert summarize_utilization(_gauge("fe", "nodejs.eventloop.utilization", 0.4, 0.1), _W)[
            ("fe", "eventloop")].sig_state == State.ABSENT

    def test_missing_window_or_zero_baseline_is_unknown(self):
        only_incident = [m for m in _gauge("ad", "jvm.cpu.recent_utilization", 0.1, 0.9) if m.ts >= _W]
        assert summarize_utilization(only_incident, _W)[("ad", "cpu")].sig_state is None
        zero_base = _gauge("ad", "jvm.cpu.recent_utilization", 0.0, 0.9)
        assert summarize_utilization(zero_base, _W)[("ad", "cpu")].sig_state is None

    def test_malformed_sample_leaves_the_metric_unmeasured(self):
        samples = _gauge("ad", "jvm.cpu.recent_utilization", 0.1, 0.12)
        samples.append(_M("ad", "jvm.cpu.recent_utilization", -1.0, _W + timedelta(seconds=99)))
        assert summarize_utilization(samples, _W)[("ad", "cpu")].sig_state is None

    def test_unattributed_samples_are_not_evidence(self):
        assert summarize_utilization(_gauge(None, "container.cpu.utilization", 0.01, 0.9), _W) == {}

    def test_self_telemetry_spike_is_not_evidence(self):
        # the product-catalog trap: SDK counters rise because the service emits more spans.
        assert summarize_utilization(_gauge("product-catalog", "otel.sdk.span.started", 10, 20), _W) == {}


class TestCpuTimeCounters:
    def test_single_monotonic_series_rate_increase_is_present(self):
        u = summarize_utilization(_counter("ad", "jvm.cpu.time", 0.01, 0.5), _W)[("ad", "cpu")]
        assert u.sig_state == State.PRESENT and u.measured

    def test_steady_rate_is_absent(self):
        assert summarize_utilization(_counter("ad", "jvm.cpu.time", 0.1, 0.11), _W)[
            ("ad", "cpu")].sig_state == State.ABSENT

    def test_interleaved_series_is_unknown(self):
        # two flattened attribute series (e.g. state=user/system) share timestamps -> rate meaningless.
        a = _counter("rec", "process.cpu.time", 0.1, 0.5)
        b = [_M(m.service, m.metric, m.value * 3 + 7, m.ts) for m in _counter("rec", "process.cpu.time", 0.1, 0.1)]
        assert summarize_utilization(a + b, _W)[("rec", "cpu")].sig_state is None

    def test_counter_reset_is_unknown(self):
        samples = _counter("ad", "jvm.cpu.time", 0.1, 0.5)
        samples.append(_M("ad", "jvm.cpu.time", 0.0, _W + timedelta(seconds=60)))  # restart
        assert summarize_utilization(samples, _W)[("ad", "cpu")].sig_state is None


class TestWitnessAcrossMetricsOfAClass:
    def test_present_metric_wins_the_class(self):
        samples = (_gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9)
                   + _counter("ad", "jvm.cpu.time", 0.1, 0.11))
        u = summarize_utilization(samples, _W)[("ad", "cpu")]
        assert u.sig_state == State.PRESENT and u.ratio > 2.0


def _util(service, resource, state):
    return UtilSignal(service, resource, 5.0 if state == State.PRESENT else 1.0, True, state)


class TestModel:
    def test_present_util_generates_its_service_with_a_util_expectation(self):
        hyps = build_hypotheses({}, set(), None, {("ad", "cpu"): _util("ad", "cpu", State.PRESENT)})
        (h,) = hyps
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
        assert [o.id for o in obs] == ["util:ad:cpu"]  # sig:ad stays UNKNOWN


class TestLocallySilentResourceFaultEndToEnd:
    def _metrics(self):
        # ad's requests look normal in traces, but its JVM CPU saturates.
        return _gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9) + _gauge("cart", "jvm.cpu.recent_utilization", 0.2, 0.2)

    def test_m2b_retains_the_saturated_service(self):
        inp = structural_signals([], self._metrics(), _W)
        res = resolve(partition(
            build_hypotheses(inp.signals, inp.edges, inp.edge_signals, inp.util_signals),
            build_observables(inp.signals, inp.edge_signals, inp.util_signals)))
        assert res.localization == ("ad",)

    def test_without_util_signals_nothing_is_generated(self):
        inp = structural_signals([], self._metrics(), _W)
        assert build_hypotheses(inp.signals, inp.edges, inp.edge_signals) == []
