"""Phase H2 (#186) — the A–E structural shadow eval bridge (candidate recall). Pure, no DB.

These measure *truth retention / candidate recall*, not structural correctness (see the module
docstring). They also pin the two sound choices the first cut got wrong: honest availability
(unmeasured → UNKNOWN, never fabricated ABSENT) and no unsound callee hard rule.
"""

from datetime import datetime, timedelta

from src.core.rca.observable import State
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
    def __init__(self, span_id, parent_span_id, service, trace_id="t"):
        self.span_id, self.parent_span_id, self.service = span_id, parent_span_id, service
        self.trace_id = trace_id


# edge -> api -> media -> {storage, transcoder}
_EDGES = {("edge", "api"), ("api", "media"), ("media", "storage"), ("media", "transcoder")}


def _sig(service, *, measured=True, anomalous=False) -> ServiceSignal:
    state = None if not measured else (State.PRESENT if anomalous else State.ABSENT)
    return ServiceSignal(service, 0.4 if anomalous else 0.01, 1.0, measured, measured, state)


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

    def test_incident_latency_without_baseline_is_unknown(self):
        # only an incident latency sample, no baseline (ratio unknowable), no error -> UNKNOWN
        samples = [_M("web", "latency_ms", 30.0, _T0 + timedelta(minutes=1))]
        sig = summarize_metrics(samples, _T0)["web"]
        assert sig.sig_state is None and sig.measured is False
        assert build_observables({"web": sig}) == []  # not fabricated as OBSERVED ABSENT

    def test_low_error_with_missing_latency_is_unknown_not_absent(self):
        # error branch measured-and-low, but latency branch unmeasured -> the OR can't be proven ABSENT
        samples = [_M("db", "error_rate", 0.01, _T0 + timedelta(minutes=1))]
        sig = summarize_metrics(samples, _T0)["db"]
        assert sig.sig_state is None  # UNKNOWN, not ABSENT
        assert "sig:db" not in {o.id for o in build_observables({"db": sig})}

    def test_both_branches_measured_and_normal_is_absent(self):
        samples = [_M("db", "error_rate", 0.01, _T0 + timedelta(minutes=1)),
                   _M("db", "latency_ms", 10.0, _T0 - timedelta(minutes=1)),
                   _M("db", "latency_ms", 11.0, _T0 + timedelta(minutes=1))]
        assert summarize_metrics(samples, _T0)["db"].sig_state == State.ABSENT

    def test_malformed_latency_cannot_produce_observed_absent(self):
        # negative incident latency is malformed telemetry, not proof of normal; the latency branch
        # is UNKNOWN, so with a measured-normal error branch the combined sig is UNKNOWN, not ABSENT.
        samples = [_M("db", "error_rate", 0.01, _T0 + timedelta(minutes=1)),
                   _M("db", "latency_ms", 10.0, _T0 - timedelta(minutes=1)),   # baseline
                   _M("db", "latency_ms", -10.0, _T0 + timedelta(minutes=1))]  # malformed incident
        sig = summarize_metrics(samples, _T0)["db"]
        assert sig.latency_measured is False and sig.sig_state is None
        assert build_observables({"db": sig}) == []                  # no fabricated ABSENT in F_usable

    def test_out_of_range_error_branch_is_unmeasured(self):
        # error_rate > 1 is not a valid rate -> the error branch is UNKNOWN, not clamped to a category
        samples = [_M("db", "error_rate", 5.0, _T0 + timedelta(minutes=1))]
        sig = summarize_metrics(samples, _T0)["db"]
        assert sig.error_measured is False and sig.sig_state is None

    def test_zero_latency_baseline_is_unknown_not_absent(self):
        # baseline latency 0 makes 30/0 undefined; with a measured-normal error branch the OR must
        # NOT be declared ABSENT — the latency branch is unavailable, so sig is UNKNOWN.
        samples = [_M("db", "error_rate", 0.01, _T0 + timedelta(minutes=1)),
                   _M("db", "latency_ms", 0.0, _T0 - timedelta(minutes=1)),   # zero baseline
                   _M("db", "latency_ms", 30.0, _T0 + timedelta(minutes=1))]
        sig = summarize_metrics(samples, _T0)["db"]
        assert sig.sig_state is None                              # UNKNOWN, not ABSENT
        assert build_observables({"db": sig}) == []              # no fabricated coordinate in F_usable


class TestCallEdges:
    def test_parent_child_service_edges(self):
        spans = [_S("s1", None, "edge"), _S("s2", "s1", "api"), _S("s3", "s2", "media")]
        assert call_edges(spans) == {("edge", "api"), ("api", "media")}

    def test_edges_do_not_cross_trace_boundaries(self):
        # both traces reuse local span id "1"; keying by span_id alone would invent other->db
        spans = [
            _S("1", None, "api", trace_id="A"), _S("2", "1", "db", trace_id="A"),
            _S("1", None, "other", trace_id="B"),
        ]
        assert call_edges(spans) == {("api", "db")}  # not {("other", "db")}


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

    def test_metricless_callee_is_in_the_service_universe(self):
        from src.eval.structural_shadow import service_universe
        # metrics mention only 'api' (anomalous); spans add a metricless callee 'db'
        signals = {"api": _sig("api", anomalous=True)}
        edges = {("api", "db")}
        hyps = build_hypotheses(signals, edges)
        localizations = {h.localization for h in hyps}
        assert "db" in localizations                                   # silent-root candidate generated
        universe = service_universe(signals, {"api", "db"})            # span_services, not edges
        assert universe == {"api", "db"}
        assert len(localizations) <= len(universe)                     # candidate_ratio <= 1.0

    def test_span_only_root_with_no_edge_counts_in_universe(self):
        from src.eval.structural_shadow import service_universe
        # 'db' appears only as a root span (no parent edge) -> still telemetry-visible
        assert service_universe({"api": _sig("api", anomalous=True)}, {"api", "db"}) == {"api", "db"}


class TestScoring:
    def test_shadow_score_reports_recall_and_selectivity(self):
        results = [
            ShadowResult("c1", "storage", "uncertain", (), ("storage", "media"), True, False, False,
                         n_candidates=2, n_services=4),
            ShadowResult("c2", "storage", "identified", (), ("storage",), True, True, False,
                         n_candidates=1, n_services=4),
            ShadowResult("c3", "storage", "uncertain", (), ("media", "api"), False, False, False,
                         n_candidates=2, n_services=4),
        ]
        s = score_shadow(results)
        assert s.n == 3
        assert round(s.candidate_recall, 3) == round(2 / 3, 3)   # c1,c2 retained truth; c3 did not
        assert round(s.unique_rate, 3) == round(1 / 3, 3)
        assert round(s.mean_candidate_ratio, 3) == round((0.5 + 0.25 + 0.5) / 3, 3)
        assert round(s.mean_candidates, 3) == round(5 / 3, 3)
        assert s.outcome_counts["uncertain"] == 2

    def test_return_everything_is_visible_as_full_ratio(self):
        # a generator that returns every service gets recall 1.0 but candidate_ratio 1.0 — not narrowing
        results = [ShadowResult("c", "storage", "uncertain", (), ("a", "b", "storage"), True, False,
                                False, n_candidates=3, n_services=3)]
        s = score_shadow(results)
        assert s.candidate_recall == 1.0 and s.mean_candidate_ratio == 1.0

    def test_truth_not_retained_is_not_counted(self):
        results = [ShadowResult("c", "storage", "uncertain", (), ("media",), False, False, False,
                                n_candidates=1, n_services=4)]
        assert score_shadow(results).candidate_recall == 0.0

    def test_no_localization_cases_count_as_abstentions(self):
        # every case produced no candidate -> abstention_rate must be 100%, not 0%
        results = [
            ShadowResult("c1", "db", "no_candidates", (), (), False, False, True, 0, 3),
            ShadowResult("c2", "db", "no_telemetry", (), (), False, False, True, 0, 0),
        ]
        s = score_shadow(results)
        assert s.abstention_rate == 1.0 and s.candidate_recall == 0.0
