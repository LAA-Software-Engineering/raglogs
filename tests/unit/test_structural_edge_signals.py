"""#209 M2a — the per-edge observable edge:{caller}->{callee}. Pure, no DB.

Pins the frozen semantics: the callee is learned from observed parent→child relations (never from
operation names), the error branch counts explicit OTLP statuses only, an edge with no incident calls
has no observation, and a failing call toward a callee that emits nothing (unreachable) is evidence
about that callee — kept as its own coordinate, never folded into sig:{callee}."""
from datetime import datetime, timedelta, timezone

from src.core.rca.observable import State
from src.core.rca.outcome import resolve
from src.core.rca.partition import partition
from src.core.rca.structural_model import (
    EdgeSignal,
    ServiceSignal,
    build_hypotheses,
    build_observables,
    learn_call_targets,
    structural_signals,
    summarize_edges,
)

_W = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
OK, ERR, UNSET = "1", "2", None
CHARGE = "oteldemo.PaymentService/Charge"


class _Span:
    def __init__(self, service, op, ts, *, trace, sid, parent=None, dur=10.0, status=UNSET):
        self.service, self.operation, self.start_time = service, op, ts
        self.trace_id, self.span_id, self.parent_span_id = trace, sid, parent
        self.duration_ms, self.status_code = dur, status


_n = 0


def _call(caller, op, callee, ts, *, dur=10.0, status=UNSET, child=True, child_status=UNSET):
    """One call: the caller's span of `op`, plus (if `child`) the callee's server span under it."""
    global _n
    _n += 1
    tr, pid = f"t{_n}", f"p{_n}"
    out = [_Span(caller, op, ts, trace=tr, sid=pid, dur=dur, status=status)]
    if child:
        out.append(_Span(callee, "handle", ts + timedelta(milliseconds=1), trace=tr, sid=f"c{_n}",
                         parent=pid, dur=dur - 1, status=child_status))
    return out


def _baseline(caller, op, callee, n=10, dur=10.0):
    spans = []
    for i in range(n):
        spans += _call(caller, op, callee, _W - timedelta(seconds=i + 1), dur=dur, status=OK)
    return spans


def _incident(caller, op, callee, n=10, **kw):
    spans = []
    for i in range(n):
        spans += _call(caller, op, callee, _W + timedelta(seconds=i + 1), **kw)
    return spans


class TestLearnCallTargets:
    def test_target_is_learned_from_observed_children(self):
        assert learn_call_targets(_baseline("checkout", CHARGE, "payment")) == {("checkout", CHARGE): "payment"}

    def test_fan_out_operation_is_ambiguous_and_unmapped(self):
        spans = _baseline("frontend", "POST", "cart", n=5) + _baseline("frontend", "POST", "ad", n=5)
        assert ("frontend", "POST") not in learn_call_targets(spans)

    def test_operation_without_cross_service_child_is_unmapped(self):
        spans = _incident("checkout", CHARGE, "payment", n=3, child=False)
        assert learn_call_targets(spans) == {}


class TestSummarizeEdges:
    def test_unreachable_callee_is_a_present_edge(self):
        # baseline: checkout->payment calls succeed with a payment child; incident: every call ERRORs
        # and payment emits nothing at all.
        spans = _baseline("checkout", CHARGE, "payment") + _incident(
            "checkout", CHARGE, "payment", status=ERR, child=False)
        e = summarize_edges(spans, _W)[("checkout", "payment")]
        assert e.sig_state == State.PRESENT and e.error_rate == 1.0
        assert e.id == "edge:checkout->payment"

    def test_slow_edge_is_present(self):
        spans = _baseline("frontend", "GetAds", "ad") + _incident("frontend", "GetAds", "ad",
                                                                  dur=40.0, status=OK)
        assert summarize_edges(spans, _W)[("frontend", "ad")].sig_state == State.PRESENT

    def test_healthy_edge_with_explicit_ok_is_absent(self):
        spans = _baseline("checkout", CHARGE, "payment") + _incident("checkout", CHARGE, "payment", status=OK)
        assert summarize_edges(spans, _W)[("checkout", "payment")].sig_state == State.ABSENT

    def test_unset_status_keeps_the_error_branch_unmeasured(self):
        # latency normal, every incident status UNSET -> UNKNOWN, never a fabricated ABSENT edge.
        spans = _baseline("checkout", CHARGE, "payment") + _incident("checkout", CHARGE, "payment")
        e = summarize_edges(spans, _W)[("checkout", "payment")]
        assert e.sig_state is None and not e.error_measured and e.latency_measured
        assert build_observables({}, {("checkout", "payment"): e}) == []

    def test_edge_not_called_during_the_incident_has_no_observation(self):
        spans = _baseline("checkout", CHARGE, "payment")
        assert summarize_edges(spans, _W) == {}

    def test_ambiguous_operation_produces_no_edge(self):
        spans = (_baseline("frontend", "POST", "cart", n=5) + _baseline("frontend", "POST", "ad", n=5)
                 + _incident("frontend", "POST", "cart", status=ERR, child=False))
        assert summarize_edges(spans, _W) == {}


def _edge(caller, callee, state):
    return EdgeSignal(caller, callee, 1.0 if state == State.PRESENT else 0.0, 1.0, True, True, state)


class TestHypothesesAndObservables:
    def test_callee_of_a_present_edge_is_a_candidate_with_an_edge_expectation(self):
        signals = {"checkout": ServiceSignal("checkout", 0.5, 1.0, True, True, State.PRESENT)}
        edge_signals = {("checkout", "payment"): _edge("checkout", "payment", State.PRESENT)}
        hyps = {h.localization: h for h in build_hypotheses(signals, set(), edge_signals)}
        assert set(hyps) == {"checkout", "payment"}
        assert hyps["payment"].predictions == {"sig:payment": State.PRESENT,
                                              "edge:checkout->payment": State.PRESENT}

    def test_absent_edge_does_not_generate_its_callee(self):
        signals = {"checkout": ServiceSignal("checkout", 0.5, 1.0, True, True, State.PRESENT)}
        edge_signals = {("checkout", "payment"): _edge("checkout", "payment", State.ABSENT)}
        assert {h.localization for h in build_hypotheses(signals, set(), edge_signals)} == {"checkout"}

    def test_without_edge_signals_the_m1_model_is_unchanged(self):
        signals = {"checkout": ServiceSignal("checkout", 0.5, 1.0, True, True, State.PRESENT)}
        (h,) = build_hypotheses(signals, set())
        assert h.predictions == {"sig:checkout": State.PRESENT}

    def test_edge_observable_is_its_own_coordinate(self):
        obs = build_observables(
            {"payment": ServiceSignal("payment", 0.0, 1.0, False, False, None)},
            {("checkout", "payment"): _edge("checkout", "payment", State.PRESENT)},
        )
        assert [o.id for o in obs] == ["edge:checkout->payment"]  # sig:payment stays UNKNOWN


class TestUnreachableCalleeEndToEnd:
    def _spans(self):
        # checkout serves the frontend; in the incident its payment calls ERROR and payment is silent.
        spans = _baseline("checkout", CHARGE, "payment", n=20)
        spans += _incident("checkout", CHARGE, "payment", n=20, status=ERR, child=False)
        return spans

    def test_m2a_retains_the_unreachable_callee(self):
        signals, edges, edge_signals, _ = structural_signals(self._spans(), [], _W)
        assert "payment" not in {c for _, c in edges}  # no incident payment span -> not in call graph
        res = resolve(partition(build_hypotheses(signals, edges, edge_signals),
                                build_observables(signals, edge_signals)))
        assert "payment" in res.localization

    def test_m1_model_alone_cannot(self):
        signals, edges, _, _ = structural_signals(self._spans(), [], _W)
        hyps = build_hypotheses(signals, edges)  # no edge signals = the M1 model
        assert "payment" not in {h.localization for h in hyps}
