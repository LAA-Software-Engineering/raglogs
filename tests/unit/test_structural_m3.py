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
    evaluate_case,
    render_markdown,
    scenario_of,
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


def _call(ts, *, status=OK, child=True, callee="payment", op=CHARGE):
    global _n
    _n += 1
    out = [_Span("frontend", op, ts, trace=f"t{_n}", sid=f"p{_n}", status=status)]
    if child:
        out.append(_Span(callee, "handle", ts + timedelta(milliseconds=1), trace=f"t{_n}", sid=f"c{_n}",
                         parent=f"p{_n}", dur=9.0, status=OK))
    return out


def _traffic(n=10, *, incident_status=OK, incident_child=True, callee="payment", op=CHARGE):
    spans = []
    for i in range(n):
        spans += _call(_W - timedelta(seconds=i + 1), callee=callee, op=op)
        spans += _call(_W + timedelta(seconds=i + 1), status=incident_status, child=incident_child,
                       callee=callee, op=op)
    return spans


def _gauge(service, metric, base, inc, n=5, attributes=_ONE):
    out = [_M(service, metric, base, _W - timedelta(seconds=10 * (i + 1)), attributes=attributes)
           for i in range(n)]
    return out + [_M(service, metric, inc, _W + timedelta(seconds=10 * (i + 1)), attributes=attributes)
                  for i in range(n)]


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

    def test_locally_silent_cpu_fault_is_recovered_by_the_util_family_only(self):
        spans = _traffic() + _traffic(callee="ad", op="oteldemo.AdService/GetAds")
        metrics = _gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9)
        c = evaluate_case("otel_adHighCpu", "ad", True, spans, metrics, _W)
        assert not c.retained("M1") and not c.retained("M2a")
        assert c.retained(FULL_ARM) and c.retained_via == ("util",)
        assert c.util_measured_for_cause and c.util_present == ("util:ad:cpu",)
        assert (c.util_samples, c.util_samples_named) == (10, 10)

    def test_unnamed_utilization_counts_against_identity_coverage(self):
        metrics = _gauge("ad", "jvm.cpu.recent_utilization", 0.01, 0.9, attributes={})
        c = evaluate_case("otel_adHighCpu", "ad", True, _traffic(), metrics, _W)
        assert (c.util_samples, c.util_samples_named) == (10, 0)
        assert not c.util_measured_for_cause  # unnamed -> UNKNOWN, never measured

    def test_healthy_window_abstains_in_every_arm(self):
        c = evaluate_case("otel_healthy_1", None, False, _traffic(),
                          _gauge("ad", "jvm.cpu.recent_utilization", 0.3, 0.31), _W)
        assert all(not c.arms[a].claims for a in ARMS)
        assert c.edges_measured == 1 and c.edges_present == 0 and c.util_present == ()


def _case(cid, cause, *, retained=True, n_loc=1, n_services=10, families=("sig",), util_measured=False,
          util_present=(), outcome="uncertain"):
    loc = ((cause,) + tuple(f"x{i}" for i in range(n_loc - 1))) if (cause and retained) else \
        tuple(f"x{i}" for i in range(n_loc))
    arm = ArmResult(outcome if loc else "no_candidates", loc)
    return CaseEval(cid, cause, cause is not None, n_services, cause is not None, {a: arm for a in ARMS},
                    cause_families=tuple(families) if cause else (), util_measured_for_cause=util_measured,
                    util_present=tuple(util_present))


def _healthy(i, *, claims=False, util_present=()):
    return _case(f"otel_healthy_{i}", None, n_loc=1 if claims else 0, util_present=util_present)


def _corpus(*, retained=(True, True), n_loc=2, healthy_claims=0, n_healthy=MIN_HEALTHY_NEGATIVES, extra=()):
    pos = [_case("otel_a", "a", retained=retained[0], n_loc=n_loc),
           _case("otel_b", "b", retained=retained[1], n_loc=n_loc)]
    neg = [_healthy(i, claims=i < healthy_claims) for i in range(n_healthy)]
    return pos + neg + list(extra)


class TestDecisionRules:
    def test_passes_all_three_pre_registered_bars(self):
        assert decide(_corpus())["generalization"] == "generalizes"

    def test_truth_retained_below_half_fails(self):
        cases = _corpus(retained=(True, False)) + [_case("otel_c", "c", retained=False)]
        assert decide(cases)["generalization"] == "does_not_generalize"

    def test_candidate_fraction_above_bound_fails(self):
        n_loc = int(CANDIDATE_FRACTION_MAX * 10) + 1  # 4/10 > 0.33
        assert decide(_corpus(n_loc=n_loc))["generalization"] == "does_not_generalize"

    def test_healthy_abstention_below_bound_fails(self):
        claims = MIN_HEALTHY_NEGATIVES - int(HEALTHY_ABSTENTION_MIN * MIN_HEALTHY_NEGATIVES) + 1
        assert decide(_corpus(healthy_claims=claims))["generalization"] == "does_not_generalize"

    def test_abstention_at_the_otel_fresh_value_passes(self):
        # 7/12 = 0.583 is the otel-fresh value the bar was set from; it must pass (>= 0.58)
        assert decide(_corpus(healthy_claims=5))["generalization"] == "generalizes"

    def test_no_healthy_windows_is_untestable_and_a_deviation(self):
        v = decide(_corpus(n_healthy=0))
        assert v["generalization"] == "untestable"
        assert any("healthy negatives" in d for d in v["protocol_deviations"])

    def test_m2b_validated_by_a_resource_fault_retained_via_util(self):
        extra = [_case("otel_adHighCpu", "ad", families=("util",), util_measured=True,
                       util_present=("util:ad:cpu",))]
        assert decide(_corpus(extra=extra))["m2b"] == "validated"

    def test_m2b_untestable_when_no_resource_fault_is_measured(self):
        extra = [_case("otel_adHighCpu", "ad", retained=False, families=(), util_measured=False)]
        assert decide(_corpus(extra=extra))["m2b"] == "untestable"

    def test_m2b_not_validated_when_measured_but_not_retained_via_util(self):
        extra = [_case("otel_chaos_recommendationCpuStress", "recommendation", families=("sig",),
                       util_measured=True)]
        assert decide(_corpus(extra=extra))["m2b"] == "not_validated"

    def test_util_present_on_a_healthy_window_blocks_validation(self):
        extra = [_case("otel_adHighCpu", "ad", families=("util",), util_measured=True),
                 _healthy(99, util_present=("util:cart:cpu",))]
        v = decide(_corpus(extra=extra))
        assert v["m2b"] == "not_validated" and v["m2b_healthy_util_present"] == ["otel_healthy_99"]

    def test_missing_resource_scenarios_are_reported_as_deviations(self):
        v = decide(_corpus())
        assert v["m2b"] == "untestable"
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
