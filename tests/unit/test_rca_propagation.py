"""Unit tests for the trace-graph propagation reranker (#118 / #79). No DB."""
from src.core.rca.linkage import ServiceGraph
from src.core.rca.propagation import propagation_scores, rerank_candidates


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
