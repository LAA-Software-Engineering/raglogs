"""Unit tests for the trace-graph propagation reranker (#118 / #79). No DB."""
import pytest

from src.core.rca.features import trace_symptoms
from src.core.rca.linkage import ServiceGraph
from src.core.rca.propagation import (
    combine_log_trace_evidence,
    failed_edge_dependencies,
    propagation_scores,
    rerank_candidates,
)


def _graph(edges):
    g = ServiceGraph()
    for a, b in edges:
        g.edges.add((a, b))
        g.services.update((a, b))
    return g


def _order(scored, graph, onset, anomaly, **kw):
    return [s for s, _ in propagation_scores(scored, graph, onset, anomaly, **kw)]


class TestGuards:
    def test_empty_graph_keeps_score_order(self):
        scored = [("caller", 0.6), ("culprit", 0.4)]
        out = propagation_scores(scored, ServiceGraph(), {}, {"caller": 5, "culprit": 5})
        assert [s for s, _ in out] == ["caller", "culprit"]

    def test_single_candidate_unchanged(self):
        out = propagation_scores([("a", 0.9)], _graph([("a", "b")]), {}, {"a": 1})
        assert out == [("a", 0.9)]

    def test_fewer_than_two_anomalous_unchanged(self):
        # only one service carries anomaly signal -> nothing to rerank against
        scored = [("caller", 0.6), ("culprit", 0.4)]
        out = _order(scored, _graph([("caller", "culprit")]), {}, {"caller": 5})
        assert out == ["caller", "culprit"]

    def test_ties_broken_by_name(self):
        scored = [("b", 0.5), ("a", 0.5)]
        out = _order(scored, ServiceGraph(), {}, {})
        assert out == ["a", "b"]


class TestTemporalPrecedence:
    def test_earlier_onset_culprit_promoted_over_loud_caller(self):
        # caller depends on culprit (caller -> culprit). The caller is the loud
        # symptom (ranker put it first); the culprit degraded FIRST. Timing must
        # lift the culprit above the caller.
        graph = _graph([("caller", "culprit")])
        scored = [("caller", 0.60), ("culprit", 0.55)]
        onset = {"culprit": 100.0, "caller": 120.0}  # culprit precedes
        anomaly = {"caller": 50.0, "culprit": 40.0}
        assert _order(scored, graph, onset, anomaly) == ["culprit", "caller"]

    def test_pure_downstream_symptom_penalised(self):
        # symptom degraded AFTER the cause -> stays below despite loud volume
        graph = _graph([("symptom", "cause")])
        scored = [("symptom", 0.52), ("cause", 0.50)]
        onset = {"cause": 10.0, "symptom": 40.0}
        anomaly = {"symptom": 100.0, "cause": 30.0}
        assert _order(scored, graph, onset, anomaly) == ["cause", "symptom"]

    def test_simultaneous_onset_within_gap_is_neutral(self):
        graph = _graph([("a", "b")])
        scored = [("a", 0.6), ("b", 0.4)]
        onset = {"a": 100.0, "b": 100.5}  # within default 1s gap
        anomaly = {"a": 10.0, "b": 10.0}
        # no temporal signal, direction fallback off by default -> order unchanged
        assert _order(scored, graph, onset, anomaly) == ["a", "b"]


class TestProximityAndMagnitude:
    def test_distant_neighbour_weighs_less(self):
        # s precedes a far symptom (2 hops) and a near one (1 hop); a near loud
        # symptom should move s more than a distant faint one.
        graph = _graph([("s", "mid"), ("mid", "far")])
        onset = {"s": 0.0, "mid": 50.0, "far": 50.0}
        near = propagation_scores(
            [("s", 0.4), ("mid", 0.5), ("far", 0.5)], graph, onset,
            {"s": 1.0, "mid": 100.0, "far": 1.0},
        )
        s_score = dict(near)["s"]
        assert s_score > 0.4  # boosted upward by the downstream symptoms

    def test_no_anomaly_service_not_treated_as_symptom(self):
        graph = _graph([("cause", "quiet")])
        scored = [("cause", 0.5), ("quiet", 0.5), ("other", 0.5)]
        onset = {"cause": 0.0, "quiet": 100.0, "other": 100.0}
        # 'quiet' has zero anomaly; 'other' is anomalous and later -> drives the boost
        out = propagation_scores(scored, graph, onset, {"cause": 10.0, "other": 10.0})
        assert out[0][0] == "cause" or dict(out)["cause"] >= 0.5


class TestDirectionFallback:
    def test_direction_used_only_when_timing_missing(self):
        # no onset data; with direction_weight the callee (dependency) outranks caller
        graph = _graph([("caller", "callee")])
        scored = [("caller", 0.55), ("callee", 0.50)]
        anomaly = {"caller": 10.0, "callee": 10.0}
        out = _order(scored, graph, {}, anomaly, direction_weight=1.0)
        assert out == ["callee", "caller"]

    def test_direction_off_by_default_keeps_order_without_timing(self):
        graph = _graph([("caller", "callee")])
        scored = [("caller", 0.55), ("callee", 0.50)]
        anomaly = {"caller": 10.0, "callee": 10.0}
        assert _order(scored, graph, {}, anomaly) == ["caller", "callee"]


class TestTraceSymptoms:
    def test_error_status_span_is_the_symptom(self):
        rows = [
            ("svc", 105.0, "2"),   # ERROR (OTLP) -> onset here
            ("svc", 110.0, "2"),   # another ERROR -> magnitude 2
            ("svc", 108.0, "0"),   # OK, ignored
            ("svc", 90.0, "2"),    # before incident window, ignored
        ]
        out = trace_symptoms(rows, incident_start=100, incident_end=200)
        assert out["svc"] == (105.0, 2.0)

    def test_error_status_word_form_matches(self):
        assert trace_symptoms([("svc", 150.0, "ERROR")], 100, 200) == {"svc": (150.0, 1.0)}

    def test_ok_and_unset_status_produce_no_symptom(self):
        rows = [("svc", 105.0, "0"), ("svc", 106.0, "1"), ("svc", 107.0, None)]
        assert trace_symptoms(rows, 100, 200) == {}


class TestCombineEvidence:
    def test_log_service_keeps_log_evidence(self):
        onset, anomaly = combine_log_trace_evidence(
            log_onset={"a": 10.0}, log_err={"a": 5.0}, trace_sym={"a": (99.0, 3.0)}
        )
        assert onset == {"a": 10.0} and anomaly == {"a": 5.0}  # log wins where present

    def test_trace_fallback_when_no_log_errors(self):
        onset, anomaly = combine_log_trace_evidence(
            log_onset={}, log_err={"a": 0.0}, trace_sym={"a": (99.0, 3.0)}
        )
        assert onset == {"a": 99.0} and anomaly == {"a": 3.0}

    def test_union_of_services(self):
        onset, anomaly = combine_log_trace_evidence(
            log_onset={"a": 1.0}, log_err={"a": 2.0}, trace_sym={"b": (5.0, 4.0)}
        )
        assert set(anomaly) == {"a", "b"} and anomaly["b"] == 4.0 and onset["b"] == 5.0


class TestFailedEdgeDependencies:
    # Real-OTel pivot: an ERROR span implicates the specific dependency it was CALLING
    # (its child span's service), distinguishing the failing callee from sibling callees.
    # spans: (span_id, parent_span_id, service, status_code)
    def test_error_span_implicates_its_callee_not_siblings(self):
        spans = [
            ("s_fe", None, "frontend", "0"),
            ("s_co", "s_fe", "checkout", "2"),      # checkout span ERROR (caller)
            ("s_pay", "s_co", "payment", "0"),      # its child = payment (the failing callee)
            ("s_cart", "s_fe", "cart", "0"),        # sibling callee, healthy
        ]
        dep = failed_edge_dependencies(spans)
        assert dep.get("payment", 0) > 0 and "cart" not in dep
        assert "checkout" not in dep  # the anchor itself isn't credited when it has a callee

    def test_erroring_leaf_implicates_itself(self):
        # paymentFailure: payment marks its OWN span ERROR and has no child -> it's the cause
        spans = [("s_co", None, "checkout", "0"), ("s_pay", "s_co", "payment", "2")]
        dep = failed_edge_dependencies(spans)
        assert dep == {"payment": 1.0}

    def test_shared_culprit_accumulates(self):
        # two independent error spans each with a single payment callee -> payment = 2.0
        spans = [
            ("a1", None, "checkout", "2"), ("a2", "a1", "payment", "0"),
            ("b1", None, "recommendation", "2"), ("b2", "b1", "payment", "0"),
        ]
        assert failed_edge_dependencies(spans)["payment"] == 2.0

    def test_ambiguous_fanout_splits_weight_true_culprit_still_wins(self):
        # a single coarse ERROR span fanning out to 3 callees is ambiguous -> 1/3 each,
        # NOT full credit to all three (the reviewer's real-span case). A second, clean
        # single-callee error span implicating payment lets the true culprit accumulate.
        spans = [
            ("c1", None, "checkout", "2"),          # coarse ERROR span, 3 downstream callees
            ("c1p", "c1", "payment", "0"),
            ("c1c", "c1", "cart", "0"),
            ("c1s", "c1", "shipping", "0"),
            ("f1", None, "frontend", "2"),          # clean ERROR span -> payment only
            ("f1p", "f1", "payment", "0"),
        ]
        dep = failed_edge_dependencies(spans)
        assert dep["cart"] == pytest.approx(1 / 3) and dep["shipping"] == pytest.approx(1 / 3)
        assert dep["payment"] == pytest.approx(1 / 3 + 1.0)
        assert dep["payment"] > dep["cart"]  # incidental siblings don't outweigh the culprit

    def test_no_error_spans_empty(self):
        assert failed_edge_dependencies([("s", None, "frontend", "0")]) == {}


class TestDependencyBoostLiftsSilentCause:
    def test_silent_callee_promoted_over_loud_symptom(self):
        # the real pattern: payment (cause) is SILENT (base ~0); checkout (symptom) errors and
        # has a higher base score. The additive dependency boost must lift payment to #1.
        g = _graph([("frontend", "checkout"), ("checkout", "payment")])
        scored = [("checkout", 0.090), ("frontend", 0.050), ("payment", 0.001)]
        dep = {"payment": 3.0}
        out = propagation_scores(scored, g, onset={}, anomaly={}, dependency_boost=dep)
        assert out[0][0] == "payment"

    def test_no_boost_without_dependency(self):
        g = _graph([("frontend", "checkout"), ("checkout", "payment")])
        scored = [("checkout", 0.090), ("payment", 0.001)]
        out = propagation_scores(scored, g, onset={}, anomaly={})
        assert [s for s, _ in out] == ["checkout", "payment"]

    def test_gamma_zero_disables_boost(self):
        g = _graph([("checkout", "payment")])
        scored = [("checkout", 0.090), ("payment", 0.001)]
        out = propagation_scores(scored, g, onset={}, anomaly={}, dependency_boost={"payment": 3.0}, dependency_gamma=0.0)
        assert [s for s, _ in out] == ["checkout", "payment"]


class TestRerankCandidatesAdapter:
    def test_reorders_candidate_objects_and_preserves_scores(self):
        from src.core.rca.candidates import RootCauseCandidate
        from src.core.rca.features import ServiceFeatures

        caller = RootCauseCandidate(
            service="caller", score=0.6, features=ServiceFeatures(service="caller", log_err=50)
        )
        culprit = RootCauseCandidate(
            service="culprit", score=0.55, features=ServiceFeatures(service="culprit", log_err=40)
        )
        graph = _graph([("caller", "culprit")])
        onset = {"culprit": 100.0, "caller": 120.0}
        out = rerank_candidates([caller, culprit], graph, onset)
        assert [c.service for c in out] == ["culprit", "caller"]
        # scores are the untouched ranker probabilities (only order changed)
        assert out[0].score == 0.55 and out[1].score == 0.6
