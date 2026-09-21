"""Phase H2 (#186) — the A–E structural shadow eval bridge (candidate recall). Pure, no DB.

These measure *truth retention / candidate recall*, not structural correctness (see the module
docstring). They also pin the two sound choices the first cut got wrong: honest availability
(unmeasured → UNKNOWN, never fabricated ABSENT) and no unsound callee hard rule.
"""

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


# edge -> api -> media -> {storage, transcoder}
_EDGES = {("edge", "api"), ("api", "media"), ("media", "storage"), ("media", "transcoder")}


def _sig(service, *, measured=True, anomalous=False) -> ServiceSignal:
    return ServiceSignal(service, 0.4 if anomalous else 0.01, 1.0, measured, anomalous)


def _signals(anomalous: set[str], svcs=("edge", "api", "media", "storage", "transcoder")):
    return {s: _sig(s, anomalous=s in anomalous) for s in svcs}


def _run(signals):
    return resolve(partition(build_hypotheses(signals, _EDGES), build_observables(signals)))


class TestAvailability:
    def test_no_incident_measurement_stays_unknown(self):
        # 'a' has only a baseline sample; 'b' has only an unrelated incident metric (cpu)
        samples = [
            _M("a", "error_rate", 0.01, _T0 - timedelta(minutes=1)),        # baseline only
            _M("b", "process.cpu.time", 5.0, _T0 + timedelta(minutes=1)),   # unrelated only
            _M("c", "error_rate", 0.4, _T0 + timedelta(minutes=1)),         # measured, anomalous
        ]
        sig = summarize_metrics(samples, _T0)
        assert sig["a"].measured is False and sig["b"].measured is False
        assert sig["c"].measured is True and sig["c"].anomalous is True
        # only the measured service yields an OBSERVED coordinate; a/b stay UNKNOWN (omitted)
        obs_ids = {o.id for o in build_observables(sig)}
        assert obs_ids == {"sig:c"}

    def test_unmeasured_service_is_not_in_f_usable(self):
        signals = {"c": _sig("c", anomalous=True), "a": _sig("a", measured=False)}
        p = partition(build_hypotheses(signals, set()), build_observables(signals))
        assert "sig:a" not in p.f_usable and "sig:c" in p.f_usable

    def test_latency_anomaly_is_measured(self):
        samples = [_M("web", "latency_ms", 10.0, _T0 - timedelta(minutes=1)),
                   _M("web", "latency_ms", 30.0, _T0 + timedelta(minutes=1))]  # 3x
        sig = summarize_metrics(samples, _T0)["web"]
        assert sig.measured is True and sig.anomalous is True


class TestCallEdges:
    def test_parent_child_service_edges(self):
        spans = [_S("s1", None, "edge"), _S("s2", "s1", "api"), _S("s3", "s2", "media")]
        assert call_edges(spans) == {("edge", "api"), ("api", "media")}


class TestCandidateRecall:
    def test_callee_fail_retains_truth(self):
        # storage (root) errors; the whole path above it is anomalous too
        res = _run(_signals({"edge", "api", "media", "storage"}))
        assert "storage" in res.localization                 # truth retained
        assert res.outcome is not Outcome.NO_COMPATIBLE_HYPOTHESIS

    def test_symptom_only_retains_silent_root(self):
        # only the symptom service (media) is anomalous; storage (root) is silent but still generated
        res = _run(_signals({"media"}))
        assert "storage" in res.localization                 # the coverage gap, recalled
        assert res.localization != ("storage",)              # not uniquely pinned

    def test_caller_origin_fault_is_not_eliminated(self):
        # the CALLER (api) is the true fault; its callee (media) is also anomalous (induced effect).
        # no unsound hard rule => api is not eliminated by media being anomalous.
        signals = _signals({"api", "media"})
        p = partition(build_hypotheses(signals, _EDGES), build_observables(signals))
        res = resolve(p)
        assert "api" in res.localization                     # truth (caller) retained, not excluded

    def test_healthy_service_is_not_a_candidate(self):
        res = _run(_signals({"media"}))
        # edge/api are healthy and not callees of the anomalous media -> never generated
        assert "edge" not in res.localization and "api" not in res.localization


class TestScoring:
    def test_shadow_score_reports_recall_not_struct_ok(self):
        results = [
            ShadowResult("c1", "storage", "uncertain", (), ("storage", "media"), True, False, False),
            ShadowResult("c2", "storage", "identified", (), ("storage",), True, True, False),
            ShadowResult("c3", "storage", "uncertain", (), ("media", "api"), False, False, False),
        ]
        s = score_shadow(results)
        assert s.n == 3
        assert round(s.candidate_recall, 3) == round(2 / 3, 3)   # c1,c2 retained truth; c3 did not
        assert round(s.unique_rate, 3) == round(1 / 3, 3)
        assert s.outcome_counts["uncertain"] == 2

    def test_truth_not_retained_is_not_counted(self):
        results = [ShadowResult("c", "storage", "uncertain", (), ("media",), False, False, False)]
        assert score_shadow(results).candidate_recall == 0.0
