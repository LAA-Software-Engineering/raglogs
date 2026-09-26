"""#209 M3 evaluator — arms, per-case attribution and the pre-registered decision rules. Pure, no DB.

The arms are nested views of one set of inputs (M1 ⊂ M2a ⊂ M2a+M2b); each family must recover exactly
the fault it exists for, and the verdicts must apply the thresholds frozen in the M3 protocol."""
from datetime import datetime, timedelta, timezone

import pytest

from src.eval.structural_m3 import (
    ARMS,
    CANDIDATE_FRACTION_MAX,
    FULL_ARM,
    HEALTHY_ABSTENTION_MIN,
    MIN_HEALTHY_NEGATIVES,
    ArmResult,
    CaseEval,
    arm_inputs,
    build_m3_report,
    decide,
    edge_specificity,
    evaluate_case,
    render_markdown,
    scenario_of,
    summarize_arm,
)

_W = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
OK, ERR = "1", "2"
CHARGE = "oteldemo.PaymentService/Charge"
_ONE = {"service.instance.id": "pod-1"}


class _Span:
    def __init__(self, service, op, ts, *, trace, sid, parent=None, dur=10.0, status=OK):
        self.service, self.operation, self.start_time = service, op, ts
        self.trace_id, self.span_id, self.parent_span_id = trace, sid, parent
        self.duration_ms, self.status_code = dur, status


class _M:
    def __init__(self, service, metric, value, ts, *, attributes=_ONE, metric_type="gauge"):
        self.service, self.metric, self.value, self.ts = service, metric, value, ts
        self.attributes, self.metric_type = attributes, metric_type


_n = 0


def _call(ts, *, status=OK, child=True, callee="payment", op=CHARGE, dur=10.0, child_status=OK):
    global _n
    _n += 1
    out = [_Span("frontend", op, ts, trace=f"t{_n}", sid=f"p{_n}", status=status, dur=dur)]
    if child:
        out.append(_Span(callee, "handle", ts + timedelta(milliseconds=1), trace=f"t{_n}", sid=f"c{_n}",
                         parent=f"p{_n}", dur=dur - 1, status=child_status))
    return out


def _traffic(n=10, *, incident_status=OK, incident_child=True, callee="payment", op=CHARGE, incident_dur=10.0,
             incident_child_status=OK, incident_errors=None):
    """``n`` baseline + ``n`` incident calls. ``incident_errors`` fails only the first k incident calls."""
    spans = []
    for i in range(n):
        spans += _call(_W - timedelta(seconds=i + 1), callee=callee, op=op)
        status = incident_status if incident_errors is None else (ERR if i < incident_errors else OK)
        spans += _call(_W + timedelta(seconds=i + 1), status=status, child=incident_child, callee=callee,
                       op=op, dur=incident_dur, child_status=incident_child_status)
    return spans


def _gauge(service, metric, base, inc, n=5, attributes=_ONE):
    out = [_M(service, metric, base, _W - timedelta(seconds=10 * (i + 1)), attributes=attributes)
           for i in range(n)]
    return out + [_M(service, metric, inc, _W + timedelta(seconds=10 * (i + 1)), attributes=attributes)
                  for i in range(n)]


_GET_ADS = "oteldemo.AdService/GetAds"


class TestArms:
    def test_unknown_arm_is_rejected(self):
        with pytest.raises(ValueError):
            arm_inputs(None, "M4")  # type: ignore[arg-type]

    def test_unreachable_callee_is_recovered_by_the_edge_family_only(self):
        # frontend's Charge calls fail and payment emits nothing: only edge:frontend->payment sees it
        spans = _traffic(incident_status=ERR, incident_child=False)
        c = evaluate_case("otel_paymentUnreachable", "payment", True, spans, [], _W)
        assert not c.retained("M1")
        assert c.retained("M2a") and c.retained(FULL_ARM)
        assert "edge" in c.retained_via

    def test_locally_silent_cpu_fault_is_retained_by_util(self):
        spans = _traffic() + _traffic(callee="ad", op=_GET_ADS)
        metrics = _gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9)
        c = evaluate_case("otel_adHighCpu", "ad", True, spans, metrics, _W)
        assert not c.retained("M1") and not c.retained("M2a")
        assert c.retained(FULL_ARM) and c.retained_via == ("util",) and c.retained_by_util
        assert c.util_measured_for_cause and c.util_present == ("util:ad:cpu",)
        assert (c.util_samples, c.util_samples_named) == (10, 10)

    def test_cpu_fault_already_retained_by_sig_is_not_retained_by_util(self):
        # ad's own spans fail (sig:ad PRESENT, so M1 already retains ad) AND its CPU is pegged: util
        # witnessed the case but did not retain it — the arm delta says so.
        spans = _traffic() + _traffic(callee="ad", op=_GET_ADS, incident_child_status=ERR)
        metrics = _gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9)
        c = evaluate_case("otel_adHighCpu", "ad", True, spans, metrics, _W)
        assert c.retained("M1") and c.retained("M2a") and c.retained(FULL_ARM)
        assert set(c.retained_via) >= {"sig", "util"}
        assert not c.retained_by_util

    def test_unnamed_utilization_counts_against_identity_coverage(self):
        metrics = _gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9, attributes={})
        c = evaluate_case("otel_adHighCpu", "ad", True, _traffic(), metrics, _W)
        assert (c.util_samples, c.util_samples_named) == (10, 0)
        assert not c.util_measured_for_cause  # unnamed -> UNKNOWN, never measured

    def test_universe_counts_metric_only_services(self):
        c = evaluate_case("otel_x", "ad", True, _traffic(), _gauge("ad", "jvm.cpu.recent_utilization", 0.3, 0.3), _W)
        assert c.n_services == 3 and c.cause_has_telemetry  # frontend, payment (spans) + ad (metrics only)

    def test_cause_absent_from_all_telemetry_is_unobservable(self):
        c = evaluate_case("otel_kafkaQueueProblems", "kafka", True, _traffic(), [], _W)
        assert not c.cause_has_telemetry

    def test_healthy_window_abstains_in_every_arm(self):
        c = evaluate_case("otel_healthy_1", None, False, _traffic(),
                          _gauge("ad", "jvm.cpu.recent_utilization", 0.3, 0.31), _W)
        assert all(not c.arms[a].generated for a in ARMS)
        assert c.edges_measured == 1 and c.edges_present == 0 and c.util_present == ()


class TestEdgeSpecificity:
    def test_error_only_edge_is_not_a_latency_flag(self):
        # 2/10 explicit errors (>= 5% cutoff), latency unchanged: PRESENT, but never crossed 2x
        c = evaluate_case("otel_healthy_1", None, False, _traffic(incident_errors=2), [], _W)
        es = edge_specificity([c])
        assert es["healthy_windows_with_present_edge"] == [1, 1]
        assert es["healthy_windows_with_error_present_edge"] == [1, 1]
        assert es["healthy_windows_with_latency_high_edge"] == [0, 1]

    def test_latency_high_is_counted_even_when_the_error_branch_also_fired(self):
        c = evaluate_case("otel_healthy_1", None, False, _traffic(incident_errors=1, incident_dur=30.0), [], _W)
        es = edge_specificity([c])
        assert es["healthy_windows_with_latency_high_edge"] == [1, 1]
        assert es["healthy_latency_high_over_latency_measured_edges"] == [1, 1]
        assert es["healthy_windows_with_error_present_edge"] == [1, 1]


def _case(cid, cause, *, retained=True, retained_m2a=None, n_loc=1, n_services=10, families=("sig",),
          util_measured=False, util_present=(), outcome="uncertain", util_samples=(0, 0), has_tel=True):
    """A hand-built CaseEval. ``retained_m2a`` (default: same as ``retained``) sets the M1/M2a arms
    independently of the frozen arm, so the arm delta is under test, not assumed."""
    def arm(kept):
        loc = ((cause,) + tuple(f"x{i}" for i in range(n_loc - 1))) if (cause and kept) else \
            tuple(f"x{i}" for i in range(n_loc))
        return ArmResult(outcome if (loc or outcome != "uncertain") else "no_candidates", loc)
    kept_m2a = retained if retained_m2a is None else retained_m2a
    arms = {"M1": arm(kept_m2a), "M2a": arm(kept_m2a), FULL_ARM: arm(retained)}
    return CaseEval(cid, cause, cause is not None, n_services, cause is not None and has_tel, arms,
                    cause_families=tuple(families) if cause else (), util_measured_for_cause=util_measured,
                    util_present=tuple(util_present), util_samples=util_samples[0],
                    util_samples_named=util_samples[1])


def _healthy(i, *, generated=False, util_present=(), outcome="uncertain"):
    return _case(f"otel_healthy_{i}", None, n_loc=1 if generated else 0, util_present=util_present,
                 outcome=outcome)


def _corpus(*, retained=(True, True), n_loc=2, healthy_generated=0, n_healthy=MIN_HEALTHY_NEGATIVES, extra=(),
            coverage=(100, 80)):
    pos = [_case("otel_a", "a", retained=retained[0], n_loc=n_loc, util_samples=coverage),
           _case("otel_b", "b", retained=retained[1], n_loc=n_loc)]
    neg = [_healthy(i, generated=i < healthy_generated) for i in range(n_healthy)]
    return pos + neg + list(extra)


def _util_fault(cid="otel_adHighCpu", cause="ad", **kw):
    return _case(cid, cause, **{"families": ("util",), "util_measured": True,
                                "util_present": (f"util:{cause}:cpu",), **kw})


class TestDecisionRules:
    def test_passes_all_three_pre_registered_bars(self):
        assert decide(_corpus())["generalization"] == "generalizes"

    def test_truth_retained_below_half_fails(self):
        cases = _corpus(retained=(True, False)) + [_case("otel_c", "c", retained=False)]
        assert decide(cases)["generalization"] == "does_not_generalize"

    def test_unobservable_cause_is_outside_the_retention_denominator(self):
        cases = _corpus() + [_case("otel_kafka", "kafka", retained=False, has_tel=False)]
        v = decide(cases)
        assert v["generalization"] == "generalizes"
        assert v["generalization_criteria"]["truth_retained_cause_has_telemetry"][0] == 1.0

    def test_candidate_fraction_above_bound_fails(self):
        n_loc = int(CANDIDATE_FRACTION_MAX * 10) + 1  # 4/10 > 0.33
        assert decide(_corpus(n_loc=n_loc))["generalization"] == "does_not_generalize"

    def test_bound_is_the_frozen_literal_so_exactly_one_third_fails(self):
        # otel-fresh M1's median fraction is exactly 1/3; the frozen text is "<= 0.33", applied literally
        assert decide([
            _case("otel_a", "a", n_loc=1, n_services=3), _case("otel_b", "b", n_loc=1, n_services=3),
            *[_healthy(i) for i in range(MIN_HEALTHY_NEGATIVES)]])["generalization"] == "does_not_generalize"

    def test_healthy_abstention_below_bound_fails(self):
        generated = MIN_HEALTHY_NEGATIVES - int(HEALTHY_ABSTENTION_MIN * MIN_HEALTHY_NEGATIVES) + 1
        assert decide(_corpus(healthy_generated=generated))["generalization"] == "does_not_generalize"

    def test_abstention_at_the_otel_fresh_value_passes(self):
        # 7/12 = 0.583 is the otel-fresh value the bar was set from; it must pass (>= 0.58)
        assert decide(_corpus(healthy_generated=5))["generalization"] == "generalizes"

    def test_abstention_is_no_hypothesis_generated_as_in_the_reference(self):
        # a healthy window that generated hypotheses but none compatible is NOT an abstention
        cases = _corpus(n_healthy=0) + [_healthy(i, outcome="no_compatible_hypothesis") for i in range(12)]
        arm = summarize_arm(cases, FULL_ARM)
        assert arm["healthy_abstained"] == [0, 12] and arm["healthy_no_claim"] == [12, 12]

    def test_selectivity_counts_generated_positives_with_an_empty_set_as_zero(self):
        cases = [_case("otel_a", "a", n_loc=4), _case("otel_b", "b", retained=False, n_loc=0,
                                                         outcome="no_compatible_hypothesis"),
                 _case("otel_c", "c", retained=False, n_loc=0, outcome="uncertain")]  # not generated
        arm = summarize_arm(cases, FULL_ARM)
        assert arm["generated"] == 2 and arm["median_candidate_fraction"] == pytest.approx(0.2)

    def test_no_healthy_windows_is_untestable_and_a_deviation(self):
        v = decide(_corpus(n_healthy=0))
        assert v["generalization"] == "untestable"
        assert any("healthy negatives" in d for d in v["protocol_deviations"])


class TestM2bRule:
    def test_validated_by_the_arm_delta(self):
        assert decide(_corpus(extra=[_util_fault(retained_m2a=False)]))["m2b"] == "validated"

    def test_util_present_on_a_resource_fault_sig_already_retained_is_not_validated(self):
        # counterexample 1: M2a (no util) retains the cause too, so util did not retain it
        fault = _util_fault(retained_m2a=True, families=("sig", "util"))
        assert decide(_corpus(extra=[fault]))["m2b"] == "not_validated"

    def test_arm_delta_without_util_on_the_cause_is_not_validated(self):
        fault = _case("otel_adHighCpu", "ad", retained_m2a=False, families=("sig",), util_measured=True)
        assert decide(_corpus(extra=[fault]))["m2b"] == "not_validated"

    def test_high_coverage_with_unmeasured_resource_causes_is_not_validated(self):
        # counterexample 2: coverage 0.8, the faults' util never measured -> testable, and it failed
        fault = _case("otel_adHighCpu", "ad", retained=False, families=(), util_measured=False)
        v = decide(_corpus(extra=[fault], coverage=(100, 80)))
        assert v["m2b"] == "not_validated" and v["m2b_identity_coverage"] == pytest.approx(0.8)

    def test_zero_identity_coverage_is_untestable(self):
        fault = _case("otel_adHighCpu", "ad", retained=False, families=(), util_measured=False)
        assert decide(_corpus(extra=[fault], coverage=(37282, 0)))["m2b"] == "untestable"

    def test_no_utilization_samples_at_all_is_untestable(self):
        assert decide(_corpus(coverage=(0, 0)))["m2b"] == "untestable"

    def test_util_present_on_a_healthy_window_blocks_validation(self):
        extra = [_util_fault(retained_m2a=False), _healthy(99, util_present=("util:cart:cpu",))]
        v = decide(_corpus(extra=extra))
        assert v["m2b"] == "not_validated" and v["m2b_healthy_util_present"] == ["otel_healthy_99"]

    def test_missing_resource_scenarios_are_a_deviation_and_cannot_validate(self):
        v = decide(_corpus())
        assert v["m2b"] == "not_validated"
        assert any("resource-fault scenarios absent" in d for d in v["protocol_deviations"])

    def test_retained_without_a_present_family_is_topology(self):
        assert _case("otel_a", "a", families=()).retained_via == ("topology",)
        assert _case("otel_a", "a", retained=False).retained_via == ()


def test_scenario_names():
    assert scenario_of("otel_adHighCpu") == "adHighCpu"
    assert scenario_of("otel_chaos_recommendationCpuStress") == "recommendationCpuStress"


def test_report_renders_every_arm_and_case():
    report = build_m3_report(_corpus())
    md = render_markdown(report, provenance={"model_commit": "abc123"})
    assert "`abc123`" in md and "**Generalization:** `generalizes`" in md
    assert all(arm in md for arm in ARMS)
    assert all(c["id"] in md for c in report["cases"])


def test_report_flags_unobservable_cases():
    cases = _corpus() + [_case("otel_kafka", "kafka", retained=False, has_tel=False)]
    md = render_markdown(build_m3_report(cases), provenance={})
    row = next(line for line in md.splitlines() if line.startswith("| otel_kafka "))
    assert "UNOBSERVABLE" in row
    assert "UNOBSERVABLE" not in next(line for line in md.splitlines() if line.startswith("| otel_a "))
