"""Unit tests for semantic cluster merging. No database, no live embedding API."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.core.clustering.baseline import compute_change_ratio
from src.core.clustering.clusterer import ClusterData, rank_and_merge_clusters
from src.core.clustering.semantic_merge import (
    maybe_semantic_merge,
    merge_semantic_clusters,
)
from src.core.embeddings.provider import DisabledEmbeddingsProvider

BASE = datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)


class FakeEmbeddingsProvider:
    def __init__(
        self,
        vectors: list[list[float]],
        *,
        fail: bool = False,
        available: bool = True,
    ) -> None:
        self.vectors = vectors
        self.fail = fail
        self.available = available
        self.calls: list[list[str]] = []

    def is_available(self) -> bool:
        return self.available

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embedding backend down")
        return self.vectors


def _cluster(
    fingerprint: str,
    message: str = "connection refused",
    count: int = 10,
    services: dict[str, int] | None = None,
    levels: dict[str, int] | None = None,
    first_seen: datetime | None = None,
    last_seen: datetime | None = None,
    baseline_count: int = 0,
    importance_score: float | None = None,
    is_trigger: bool = False,
    log_entry_ids: list | None = None,
) -> ClusterData:
    return ClusterData(
        fingerprint=fingerprint,
        representative_message=message,
        count=count,
        services=services if services is not None else {"api": count},
        levels=levels if levels is not None else {"error": count},
        first_seen=first_seen if first_seen is not None else BASE,
        last_seen=last_seen if last_seen is not None else BASE + timedelta(minutes=10),
        baseline_count=baseline_count,
        change_ratio=compute_change_ratio(count, baseline_count),
        importance_score=float(count) if importance_score is None else importance_score,
        is_trigger=is_trigger,
        log_entry_ids=log_entry_ids if log_entry_ids is not None else [uuid4() for _ in range(min(count, 3))],
    )


NEAR_DUP_A = [1.0, 0.0]
NEAR_DUP_B = [0.999, 0.001]
ORTHOGONAL = [0.0, 1.0]


class TestMergeSemanticClusters:
    def test_near_duplicates_merge_counts_and_services(self) -> None:
        a = _cluster("aaaa", "timeout talking to stripe", count=10, services={"api": 10}, importance_score=8.0)
        b = _cluster(
            "bbbb",
            "timeout while calling stripe api",
            count=20,
            services={"billing": 20},
            levels={"error": 15, "warn": 5},
            first_seen=BASE + timedelta(minutes=5),
            last_seen=BASE + timedelta(minutes=40),
            baseline_count=3,
            importance_score=5.0,
        )
        merged = merge_semantic_clusters([a, b], [NEAR_DUP_A, NEAR_DUP_B], 0.92)

        assert len(merged) == 1
        cluster = merged[0]
        assert cluster.count == 30
        assert cluster.fingerprint == "aaaa"
        assert cluster.merged_fingerprints == ["bbbb"]
        assert cluster.services == {"api": 10, "billing": 20}
        assert cluster.levels == {"error": 25, "warn": 5}
        assert cluster.first_seen == BASE
        assert cluster.last_seen == BASE + timedelta(minutes=40)
        assert cluster.baseline_count == 3
        assert cluster.change_ratio == pytest.approx(compute_change_ratio(30, 3))
        assert cluster.representative_message == a.representative_message
        assert cluster.log_entry_ids == a.log_entry_ids + b.log_entry_ids

    def test_grounded_counts_ten_plus_twenty(self) -> None:
        a = _cluster("fp1", count=10)
        b = _cluster("fp2", count=20)
        merged = merge_semantic_clusters([a, b], [NEAR_DUP_A, NEAR_DUP_B], 0.92)
        assert merged[0].count == 30

    def test_distinct_vectors_stay_separate(self) -> None:
        a = _cluster("aaaa", "stripe signature failed")
        b = _cluster("bbbb", "disk full on /var")
        merged = merge_semantic_clusters([a, b], [NEAR_DUP_A, ORTHOGONAL], 0.92)
        assert merged == [a, b]
        assert merged[0] is a
        assert merged[1] is b
        assert merged[0].merged_fingerprints == []

    def test_connected_component_merges_chain(self) -> None:
        a = _cluster("a", count=1, importance_score=3.0)
        b = _cluster("b", count=1, importance_score=2.0)
        c = _cluster("c", count=1, importance_score=1.0)
        # A~B and B~C above 0.92; A·C = 0.85 is below threshold.
        vecs = [[1.0, 0.0], [0.96, 0.28], [0.85, 0.53]]
        merged = merge_semantic_clusters([a, b, c], vecs, 0.92)
        assert len(merged) == 1
        assert merged[0].count == 3
        assert merged[0].fingerprint == "a"
        assert merged[0].merged_fingerprints == ["b", "c"]

    def test_empty_and_single_cluster_are_noop(self) -> None:
        assert merge_semantic_clusters([], [], 0.92) == []
        only = _cluster("only")
        clusters = [only]
        result = merge_semantic_clusters(clusters, [NEAR_DUP_A], 0.92)
        assert result is clusters
        assert result[0] is only

    def test_empty_message_does_not_merge(self) -> None:
        a = _cluster("a", message="   ")
        b = _cluster("b", message="")
        merged = merge_semantic_clusters([a, b], [NEAR_DUP_A, NEAR_DUP_B], 0.92)
        assert merged == [a, b]

    def test_min_count_skips_small_clusters(self) -> None:
        a = _cluster("a", count=1)
        b = _cluster("b", count=1)
        merged = merge_semantic_clusters([a, b], [NEAR_DUP_A, NEAR_DUP_B], 0.92, min_count=2)
        assert merged == [a, b]

    def test_mismatched_embeddings_raise(self) -> None:
        a = _cluster("a")
        b = _cluster("b")
        with pytest.raises(ValueError, match="embeddings length"):
            merge_semantic_clusters([a, b], [NEAR_DUP_A], 0.92)

    def test_is_trigger_any_member(self) -> None:
        a = _cluster("a", count=10, is_trigger=False, importance_score=9.0)
        b = _cluster("b", count=5, is_trigger=True, importance_score=1.0)
        merged = merge_semantic_clusters([a, b], [NEAR_DUP_A, NEAR_DUP_B], 0.92)
        assert merged[0].is_trigger is True

    def test_canonical_is_highest_importance(self) -> None:
        a = _cluster("low", count=100, importance_score=1.0)
        b = _cluster("high", count=2, importance_score=50.0)
        merged = merge_semantic_clusters([a, b], [NEAR_DUP_A, NEAR_DUP_B], 0.92)
        assert merged[0].fingerprint == "high"
        assert merged[0].representative_message == b.representative_message
        assert merged[0].merged_fingerprints == ["low"]


class TestMaybeSemanticMerge:
    def test_disabled_provider_is_noop(self) -> None:
        a = _cluster("a")
        b = _cluster("b")
        clusters = [a, b]
        out, used = maybe_semantic_merge(clusters, provider=DisabledEmbeddingsProvider())
        assert used is False
        assert out is clusters
        assert out[0] is a
        assert out[1] is b

    def test_unavailable_provider_is_noop(self) -> None:
        a = _cluster("a")
        b = _cluster("b")
        provider = FakeEmbeddingsProvider([], available=False)
        out, used = maybe_semantic_merge([a, b], provider=provider)
        assert used is False
        assert provider.calls == []
        assert out == [a, b]

    def test_mocked_embeddings_merge(self) -> None:
        a = _cluster("a", count=10)
        b = _cluster("b", count=20)
        provider = FakeEmbeddingsProvider([NEAR_DUP_A, NEAR_DUP_B])
        out, used = maybe_semantic_merge([a, b], provider=provider, similarity_threshold=0.92)
        assert used is True
        assert len(out) == 1
        assert out[0].count == 30
        assert provider.calls == [[a.representative_message, b.representative_message]]

    def test_provider_exception_fail_open(self) -> None:
        a = _cluster("a")
        b = _cluster("b")
        clusters = [a, b]
        provider = FakeEmbeddingsProvider([], fail=True)
        out, used = maybe_semantic_merge(clusters, provider=provider)
        assert used is False
        assert out is clusters

    def test_wrong_vector_count_fail_open(self) -> None:
        a = _cluster("a")
        b = _cluster("b")
        provider = FakeEmbeddingsProvider([NEAR_DUP_A])  # one vector for two clusters
        out, used = maybe_semantic_merge([a, b], provider=provider)
        assert used is False
        assert out == [a, b]

    def test_single_cluster_does_not_call_provider(self) -> None:
        only = _cluster("only")
        provider = FakeEmbeddingsProvider([[1.0]])
        out, used = maybe_semantic_merge([only], provider=provider)
        assert used is False
        assert provider.calls == []
        assert out == [only]

    def test_empty_clusters(self) -> None:
        out, used = maybe_semantic_merge([], provider=FakeEmbeddingsProvider([]))
        assert out == []
        assert used is False

    def test_used_embeddings_even_when_nothing_merges(self) -> None:
        a = _cluster("a")
        b = _cluster("b")
        provider = FakeEmbeddingsProvider([NEAR_DUP_A, ORTHOGONAL])
        out, used = maybe_semantic_merge([a, b], provider=provider)
        assert used is True
        assert out == [a, b]


class TestRankAndMergeClusters:
    def test_disabled_keeps_fingerprint_algorithm(self) -> None:
        high = _cluster("high", count=50, importance_score=50.0)
        low = _cluster("low", count=3, importance_score=3.0)
        out, algorithm = rank_and_merge_clusters(
            [low, high],
            max_clusters=10,
            provider=DisabledEmbeddingsProvider(),
        )
        assert algorithm == "fingerprint"
        assert [c.fingerprint for c in out] == ["high", "low"]

    def test_semantic_algorithm_when_embeddings_used(self) -> None:
        a = _cluster("a", count=10)
        b = _cluster("b", count=20)
        out, algorithm = rank_and_merge_clusters(
            [a, b],
            max_clusters=10,
            provider=FakeEmbeddingsProvider([NEAR_DUP_A, NEAR_DUP_B]),
        )
        assert algorithm == "fingerprint+semantic"
        assert len(out) == 1
        assert out[0].count == 30

    def test_max_clusters_applied_after_merge(self) -> None:
        a = _cluster("a", count=10, importance_score=10.0)
        b = _cluster("b", count=10, importance_score=9.0)
        c = _cluster("c", count=100, importance_score=1.0, message="unrelated")
        out, algorithm = rank_and_merge_clusters(
            [a, b, c],
            max_clusters=1,
            provider=FakeEmbeddingsProvider([NEAR_DUP_A, NEAR_DUP_B, ORTHOGONAL]),
        )
        assert algorithm == "fingerprint+semantic"
        assert len(out) == 1
        # Merged a+b has higher recomputed importance than the leftover cluster.
        assert out[0].count == 20
        assert out[0].fingerprint in {"a", "b"}
