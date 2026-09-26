"""#209 M1 — per-service sig derived from spans, the span+metric combiner, and the incident call graph.
Pure, no DB. Pins the UNKNOWN discipline: missing telemetry (UNSET status, a missing baseline) never
becomes a measured branch, and never a fabricated ABSENT."""
from datetime import datetime, timedelta, timezone

from src.core.rca.observable import State
from src.core.rca.structural_model import (
    ServiceSignal,
    build_hypotheses,
    build_observables,
    call_edges,
    combine_signals,
    structural_signals,
    summarize_spans,
)

_W = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)  # window_start

OK, ERR, UNSET = "1", "2", None


class _Span:
    def __init__(self, service, ts, duration_ms=10.0, status_code=None, *,
                 trace_id="t", span_id=None, parent_span_id=None):
        self.service, self.start_time = service, ts
        self.duration_ms, self.status_code = duration_ms, status_code
        self.trace_id, self.span_id, self.parent_span_id = trace_id, span_id, parent_span_id


def _stream(service, *, base_ms=10.0, base_n=20, inc_ms=10.0, inc_n=20, statuses=None):
    """Baseline spans at `base_ms`; incident spans at `inc_ms` with per-span `statuses` (default UNSET)."""
    statuses = statuses if statuses is not None else [UNSET] * inc_n
    out = [_Span(service, _W - timedelta(seconds=i + 1), duration_ms=base_ms) for i in range(base_n)]
    out += [_Span(service, _W + timedelta(seconds=i), duration_ms=inc_ms, status_code=statuses[i])
            for i in range(inc_n)]
    return out


class TestErrorBranchUsesExplicitStatusOnly:
    def test_unset_status_with_normal_latency_is_unknown(self):
        # All incident spans UNSET, latency flat: the error branch was never measured, so latency-normal
        # alone must not manufacture ABSENT (mirrors a service with no error_rate metric samples).
        sig = summarize_spans(_stream("api"), _W)["api"]
        assert sig.sig_state is None
        assert sig.error_measured is False and sig.latency_measured is True
        assert build_observables({"api": sig}) == []  # no sig:api coordinate enters F_usable

    def test_explicit_ok_with_normal_latency_is_absent(self):
        # Proven healthy needs an explicit OK status plus measured-normal latency.
        sig = summarize_spans(_stream("api", statuses=[OK] * 20), _W)["api"]
        assert sig.sig_state == State.ABSENT and sig.error_measured

    def test_sparse_errors_among_unset_are_present_not_absent(self):
        # 40 ERROR spans among 1000, the rest UNSET, latency flat. UNSET is not in the denominator, so
        # the forty failures are not diluted below the cutoff and are never "proven healthy".
        statuses = [ERR] * 40 + [UNSET] * 960
        sig = summarize_spans(_stream("api", inc_n=1000, statuses=statuses), _W)["api"]
        assert sig.sig_state == State.PRESENT
        assert sig.error_rate == 1.0  # 40 / 40 explicit statuses

    def test_error_rate_over_explicit_statuses(self):
        # 6 ERROR / 20 explicit (14 OK) -> 30% -> PRESENT via the error branch.
        sig = summarize_spans(_stream("api", statuses=[ERR] * 6 + [OK] * 14), _W)["api"]
        assert sig.sig_state == State.PRESENT and abs(sig.error_rate - 0.3) < 1e-9

    def test_no_baseline_durations_stays_unknown(self):
        sig = summarize_spans(_stream("api", base_n=0, statuses=[OK] * 20), _W)["api"]
        assert sig.sig_state is None and sig.error_measured and not sig.latency_measured


class TestLatencyBranchIsRobustToTheTail:
    def test_one_slow_span_among_twenty_does_not_flag(self):
        spans = _stream("api", statuses=[OK] * 20)
        spans.append(_Span("api", _W + timedelta(seconds=50), duration_ms=10_000.0, status_code=OK))
        assert summarize_spans(spans, _W)["api"].sig_state == State.ABSENT

    def test_one_timeout_among_a_thousand_does_not_flag(self):
        # A mean would read 10ms + 10s/1000 ~= 20ms -> 2x -> HIGH. The median does not move.
        spans = _stream("api", inc_n=1000, statuses=[OK] * 1000)
        spans.append(_Span("api", _W + timedelta(seconds=5000), duration_ms=10_000.0, status_code=OK))
        assert summarize_spans(spans, _W)["api"].sig_state == State.ABSENT

    def test_whole_distribution_shift_is_present(self):
        sig = summarize_spans(_stream("api", inc_ms=30.0), _W)["api"]
        assert sig.sig_state == State.PRESENT and sig.latency_measured

    def test_invalid_durations_are_dropped(self):
        spans = _stream("api", inc_ms=40.0)
        spans.append(_Span("api", _W + timedelta(seconds=99), duration_ms=float("nan")))
        spans.append(_Span("api", _W + timedelta(seconds=98), duration_ms=-5.0))
        assert summarize_spans(spans, _W)["api"].sig_state == State.PRESENT

    def test_never_seen_service_absent_from_signals(self):
        assert "ghost" not in summarize_spans(_stream("api"), _W)


class TestCombineSignalsCarriesTheWitness:
    def test_winning_modality_numbers_and_flags_travel_with_the_state(self):
        # Spans measured the service normal; metrics prove an anomaly. The merged record must be the
        # metric witness verbatim — not PRESENT carrying the span's 0.0 / 1.1.
        spans = {"a": ServiceSignal("a", 0.0, 1.1, True, True, State.ABSENT)}
        metrics = {"a": ServiceSignal("a", 0.4, 1.0, True, False, State.PRESENT)}
        merged = combine_signals(spans, metrics)["a"]
        assert merged.sig_state == State.PRESENT
        assert merged.error_rate == 0.4 and merged.latency_ratio == 1.0
        assert merged.latency_measured is False  # not OR'd in from the discarded span record

    def test_absent_witness_over_unknown(self):
        merged = combine_signals({"a": ServiceSignal("a", 0.0, 1.0, True, False, None)},
                                 {"a": ServiceSignal("a", 0.01, 1.1, True, True, State.ABSENT)})["a"]
        assert merged.sig_state == State.ABSENT and merged.error_rate == 0.01

    def test_all_unknown_stays_unknown_and_flags_are_not_ored(self):
        # One modality measured only errors, the other only latency; neither alone proves anything.
        merged = combine_signals({"a": ServiceSignal("a", 0.0, 1.0, True, False, None)},
                                 {"a": ServiceSignal("a", 0.0, 1.0, False, True, None)})["a"]
        assert merged.sig_state is None
        assert (merged.error_measured, merged.latency_measured) == (True, False)

    def test_union_of_services(self):
        merged = combine_signals({"a": ServiceSignal("a", 0.4, 1.0, True, True, State.PRESENT)},
                                 {"b": ServiceSignal("b", 0.0, 1.0, True, True, State.ABSENT)})
        assert set(merged) == {"a", "b"}


def _call(parent_svc, child_svc, trace, pid, cid, ts):
    return [_Span(parent_svc, ts, trace_id=trace, span_id=pid),
            _Span(child_svc, ts + timedelta(milliseconds=2), trace_id=trace, span_id=cid, parent_span_id=pid)]


class TestIncidentCallGraph:
    def _spans(self):
        before = _W - timedelta(seconds=30)
        spans = []
        spans += _call("checkout", "payment", "b1", "p1", "c1", before)  # baseline
        spans += _call("checkout", "fraud", "b2", "p2", "c2", before)    # baseline only
        spans += _call("checkout", "payment", "i1", "p3", "c3", _W + timedelta(seconds=5))  # incident
        return spans

    def test_baseline_only_edge_is_not_an_incident_edge(self):
        assert call_edges(self._spans(), since=_W) == {("checkout", "payment")}
        assert call_edges(self._spans()) == {("checkout", "payment"), ("checkout", "fraud")}  # no cutoff

    def test_baseline_parent_still_resolves_for_an_incident_child(self):
        parent = _Span("checkout", _W - timedelta(seconds=1), trace_id="x", span_id="p")
        child = _Span("db", _W + timedelta(seconds=1), trace_id="x", span_id="c", parent_span_id="p")
        assert call_edges([parent, child], since=_W) == {("checkout", "db")}

    def test_baseline_neighbour_is_not_generated_as_a_candidate(self):
        # checkout PRESENT, payment not anomalous: the callee expansion is the INCIDENT callees only.
        signals = {"checkout": ServiceSignal("checkout", 0.0, 4.0, True, True, State.PRESENT)}
        hyps = build_hypotheses(signals, call_edges(self._spans(), since=_W))
        assert {h.localization for h in hyps} == {"checkout", "payment"}  # not fraud


class TestSharedBuilder:
    def test_structural_signals_merges_modalities_and_uses_the_incident_graph(self):
        spans = _stream("checkout", inc_ms=40.0) + TestIncidentCallGraph()._spans()
        signals, edges = structural_signals(spans, [], _W)
        assert signals["checkout"].sig_state == State.PRESENT
        assert ("checkout", "fraud") not in edges
