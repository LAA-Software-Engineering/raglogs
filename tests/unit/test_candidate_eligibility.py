"""Candidate-eligibility boundary for the failure taxonomy (#186 review).

The generated-candidate set must come from the active generation boundary, not from the
display/evidence `services_affected` field: informational-only clusters and excluded services were
never eligible candidates, so a labeled cause outside the eligible set is COVERAGE, not INFERENCE.
"""

from datetime import datetime

from src.core.clustering.clusterer import ClusterData
from src.core.explain.evidence import legacy_candidate_services, select_significant_clusters
from src.eval.metrics import Prediction
from src.eval.taxonomy import Bucket, classify_failure
from tests.unit.test_taxonomy import _case  # reuse the case builder

_T0 = datetime(2026, 1, 1)


def _cluster(service: str, level: str) -> ClusterData:
    return ClusterData(
        fingerprint=f"fp-{service}-{level}", representative_message="m", count=3,
        services={service: 3}, levels={level: 3}, first_seen=_T0, last_seen=_T0,
        baseline_count=0, change_ratio=1.0, importance_score=1.0,
    )


class TestSignificantClusters:
    def test_info_only_clusters_are_not_candidates(self):
        clusters = [_cluster("db", "error"), _cluster("noisy-info", "info")]
        sig = select_significant_clusters(clusters)
        assert [c.services for c in sig] == [{"db": 3}]                 # info-only dropped
        assert legacy_candidate_services(sig) == ["db"]                 # noisy-info not a candidate

    def test_falls_back_to_all_when_none_significant(self):
        clusters = [_cluster("a", "info"), _cluster("b", "debug")]
        assert legacy_candidate_services(select_significant_clusters(clusters)) == ["a", "b"]


class TestTaxonomyRespectsEligibility:
    def test_label_only_in_info_cluster_is_coverage_not_inference(self):
        # 'db' appeared only in an informational cluster -> never a candidate -> COVERAGE.
        pred = Prediction(produced_explanation=True, root_cause_service="api",
                          predicted_services=["api"], generated_candidates=["api"])
        assert classify_failure(_case("c", service="db"), pred) is Bucket.COVERAGE

    def test_excluded_service_is_coverage_not_inference(self):
        # the learned path's build_candidates(exclude=...) drops 'db' from generated_candidates,
        # so even though it may appear in logs, the active generator never generated it -> COVERAGE.
        pred = Prediction(produced_explanation=True, root_cause_service="api",
                          predicted_services=["api", "cache"],
                          generated_candidates=["api", "cache"])  # db excluded upstream
        assert classify_failure(_case("c", service="db"), pred) is Bucket.COVERAGE
