"""Phase H2 (#186) — the A–E structural shadow eval bridge. Pure, no DB."""

from datetime import datetime, timedelta

from src.core.rca.outcome import Outcome, resolve
from src.core.rca.partition import partition
from src.eval.structural_shadow import (
    ServiceSignal,
    ShadowResult,
    build_hypotheses,
    build_observables,
    call_edges,
    score_shadow,
    summarize_metrics,
)

_T0 = datetime(2026, 1, 1, 12, 0)


class _M:  # minimal ParsedMetricSample stand-in
    def __init__(self, service, metric, value, ts):
        self.service, self.metric, self.value, self.ts = service, metric, value, ts


class _S:  # minimal ParsedSpan stand-in
    def __init__(self, span_id, parent_span_id, service):
        self.span_id, self.parent_span_id, self.service = span_id, parent_span_id, service


# The trace-loc topology: edge -> api -> media -> {storage, transcoder}
_EDGES = {("edge", "api"), ("api", "media"), ("media", "storage"), ("media", "transcoder")}


def _signals(anomalous: set[str]) -> dict[str, ServiceSignal]:
    svcs = ["edge", "api", "media", "storage", "transcoder"]
    return {s: ServiceSignal(s, 0.4 if s in anomalous else 0.01, 1.0, s in anomalous) for s in svcs}


def _run(signals):
    p = partition(build_hypotheses(signals, _EDGES), build_observables(signals))
    return resolve(p)


class TestSummarize:
    def test_incident_vs_baseline_and_anomaly(self):
        samples = [
            _M("db", "error_rate", 0.01, _T0 - timedelta(minutes=1)),   # baseline
            _M("db", "error_rate", 0.4, _T0 + timedelta(minutes=1)),    # incident -> present
            _M("web", "latency_ms", 10.0, _T0 - timedelta(minutes=1)),  # baseline
            _M("web", "latency_ms", 30.0, _T0 + timedelta(minutes=1)),  # 3x -> HIGH
        ]
        sig = summarize_metrics(samples, _T0)
        assert sig["db"].anomalous is True                 # error rate present
        assert sig["web"].anomalous is True                # latency >= 2x
        assert round(sig["web"].latency_ratio, 1) == 3.0

    def test_healthy_service_is_not_anomalous(self):
        samples = [_M("api", "error_rate", 0.01, _T0 + timedelta(minutes=1)),
                   _M("api", "latency_ms", 11.0, _T0 - timedelta(minutes=1)),
                   _M("api", "latency_ms", 12.0, _T0 + timedelta(minutes=1))]
        assert summarize_metrics(samples, _T0)["api"].anomalous is False


class TestCallEdges:
    def test_parent_child_service_edges(self):
        spans = [_S("s1", None, "edge"), _S("s2", "s1", "api"), _S("s3", "s2", "media")]
        assert call_edges(spans) == {("edge", "api"), ("api", "media")}


class TestStructuralOutcomes:
    def test_callee_fail_is_identified_at_the_root(self):
        # the root (storage) emits its own error; the whole path above it is anomalous too
        res = _run(_signals({"edge", "api", "media", "storage"}))
        assert res.outcome is Outcome.IDENTIFIED
        assert res.localization == ("storage",)  # callers hard-eliminated up the path

    def test_symptom_only_retains_truth_but_is_not_unique(self):
        # only the symptom service (media) is anomalous; the root (storage) is silent
        res = _run(_signals({"media"}))
        assert res.outcome is Outcome.UNCERTAIN
        assert "storage" in res.localization      # truth retained (struct_ok)
        assert res.localization != ("storage",)   # ...but not uniquely pinned

    def test_healthy_sibling_callee_does_not_pollute_identification(self):
        # storage emits error, transcoder (its sibling under media) is silent -> still IDENTIFIED
        res = _run(_signals({"edge", "api", "media", "storage"}))
        assert "transcoder" not in res.localization


class TestScoring:
    def test_shadow_score_aggregates(self):
        results = [
            ShadowResult("c1", "storage", "identified", ("storage",), True, True, False),
            ShadowResult("c2", "storage", "uncertain", ("storage", "transcoder"), True, False, False),
            ShadowResult("c3", "storage", "no_compatible_hypothesis", (), False, False, True),
        ]
        s = score_shadow(results)
        assert s.n == 3
        assert round(s.struct_ok_rate, 3) == round(2 / 3, 3)
        assert round(s.unique_rate, 3) == round(1 / 3, 3)
        assert round(s.abstention_rate, 3) == round(1 / 3, 3)
        assert s.outcome_counts["identified"] == 1
