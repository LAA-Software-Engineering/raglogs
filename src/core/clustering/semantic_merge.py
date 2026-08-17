"""Semantic merge of fingerprint clusters via embedding similarity.

This is an analysis-time pass after fingerprint grouping. It does not
require pgvector: vectors are computed in memory from each cluster's
representative message. When embeddings are disabled or unavailable the
pass is a no-op so clustering stays fingerprint-only and deterministic.
"""

from collections import defaultdict
from datetime import datetime
from uuid import UUID

import numpy as np

from src.config import get_settings
from src.core.clustering.baseline import compute_change_ratio
from src.core.clustering.clusterer import ClusterData
from src.core.clustering.scoring import compute_importance_score
from src.core.embeddings.provider import EmbeddingsProvider, get_embeddings_provider
from src.core.normalization.patterns import is_trigger_message


def merge_semantic_clusters(
    clusters: list[ClusterData],
    embeddings: list[list[float]],
    similarity_threshold: float,
    min_count: int = 1,
) -> list[ClusterData]:
    """Merge clusters whose representative embeddings are cosine-similar.

    Union-find / connected components: every pair with cosine similarity
    >= ``similarity_threshold`` is linked, then each component collapses
    into one ``ClusterData``. Clusters with ``count < min_count`` or an
    empty representative message never form edges.

    Grounded counts: merged ``count`` is the sum of member counts;
    services/levels are summed; ``first_seen`` is min, ``last_seen`` is max.
    """
    if len(clusters) < 2:
        return clusters
    if len(embeddings) != len(clusters):
        raise ValueError(
            f"embeddings length {len(embeddings)} does not match cluster count {len(clusters)}"
        )

    parent = list(range(len(clusters)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        parent[rb] = ra
        return True

    eligible = [_is_merge_eligible(clusters[i], embeddings[i], min_count) for i in range(len(clusters))]
    sim_matrix = _cosine_matrix(embeddings)
    merged_any = False

    for i in range(len(clusters)):
        if not eligible[i]:
            continue
        for j in range(i + 1, len(clusters)):
            if not eligible[j]:
                continue
            if float(sim_matrix[i, j]) >= similarity_threshold and union(i, j):
                merged_any = True

    if not merged_any:
        return clusters

    components: dict[int, list[int]] = defaultdict(list)
    for i in range(len(clusters)):
        components[find(i)].append(i)

    # Preserve original relative order of the first member of each component.
    ordered_roots = sorted(components, key=lambda root: min(components[root]))
    merged: list[ClusterData] = []
    for root in ordered_roots:
        members = [clusters[i] for i in components[root]]
        if len(members) == 1:
            merged.append(members[0])
        else:
            merged.append(_merge_component(members))
    return merged


def maybe_semantic_merge(
    clusters: list[ClusterData],
    *,
    provider: EmbeddingsProvider | None = None,
    similarity_threshold: float | None = None,
    min_count: int | None = None,
) -> tuple[list[ClusterData], bool]:
    """Run the semantic merge pass, failing open on any backend error.

    Returns ``(clusters, used_embeddings)``. ``used_embeddings`` is True
    only when an available provider actually embedded representatives.
    Disabled / unavailable providers, fewer than two clusters, and
    exceptions all leave the cluster list unchanged and return False.
    """
    if len(clusters) < 2:
        return clusters, False

    try:
        settings = get_settings()
        if provider is None:
            provider = get_embeddings_provider(settings)
        if not provider.is_available():
            return clusters, False

        threshold = (
            settings.cluster_merge_similarity_threshold
            if similarity_threshold is None
            else similarity_threshold
        )
        size_floor = settings.cluster_merge_min_count if min_count is None else min_count

        texts = [c.representative_message or "" for c in clusters]
        embeddings = provider.embed_texts(texts)
        if len(embeddings) != len(clusters):
            return clusters, False

        merged = merge_semantic_clusters(
            clusters,
            embeddings,
            similarity_threshold=threshold,
            min_count=size_floor,
        )
        return merged, True
    except Exception:  # noqa: BLE001 — fail open: fingerprint clustering must still run
        return clusters, False


def _is_merge_eligible(cluster: ClusterData, embedding: list[float], min_count: int) -> bool:
    if cluster.count < min_count:
        return False
    if not (cluster.representative_message or "").strip():
        return False
    if not embedding:
        return False
    return any(float(v) != 0.0 for v in embedding)


def _cosine_matrix(embeddings: list[list[float]]) -> np.ndarray:
    if not embeddings:
        return np.zeros((0, 0), dtype=np.float64)
    dim = len(embeddings[0])
    if dim == 0 or any(len(row) != dim for row in embeddings):
        raise ValueError("embedding vectors must be non-empty and of equal dimension")
    arr = np.asarray(embeddings, dtype=np.float64)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    normalized = arr / norms
    return normalized @ normalized.T


def _sum_counts(dicts: list[dict[str, int]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for d in dicts:
        for key, value in d.items():
            totals[key] = totals.get(key, 0) + value
    return totals


def _merge_component(members: list[ClusterData]) -> ClusterData:
    canonical = min(
        members,
        key=lambda c: (-c.importance_score, -c.count, c.fingerprint),
    )
    count = sum(m.count for m in members)
    baseline_count = sum(m.baseline_count for m in members)
    change_ratio = compute_change_ratio(count, baseline_count)
    services = _sum_counts([m.services for m in members])
    levels = _sum_counts([m.levels for m in members])

    firsts = [m.first_seen for m in members if m.first_seen is not None]
    lasts = [m.last_seen for m in members if m.last_seen is not None]
    first_seen: datetime | None = min(firsts) if firsts else None
    last_seen: datetime | None = max(lasts) if lasts else None

    log_entry_ids: list[UUID] = list(canonical.log_entry_ids)
    for member in members:
        if member is canonical:
            continue
        log_entry_ids.extend(member.log_entry_ids)

    merged_fingerprints = sorted(
        m.fingerprint for m in members if m.fingerprint != canonical.fingerprint
    )
    is_trigger = any(m.is_trigger for m in members) or is_trigger_message(
        canonical.representative_message or ""
    )
    importance = compute_importance_score(
        count=count,
        levels_distribution=levels,
        change_ratio=change_ratio,
        services_count=len(services),
        is_trigger_correlated=is_trigger,
    )
    return ClusterData(
        fingerprint=canonical.fingerprint,
        representative_message=canonical.representative_message,
        count=count,
        services=services,
        levels=levels,
        first_seen=first_seen,
        last_seen=last_seen,
        baseline_count=baseline_count,
        change_ratio=change_ratio,
        importance_score=importance,
        is_trigger=is_trigger,
        log_entry_ids=log_entry_ids,
        merged_fingerprints=merged_fingerprints,
    )
