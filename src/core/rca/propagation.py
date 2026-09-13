"""Trace-graph propagation reranker (#118 / #79 carve-out).

The frozen external result (33% top-1 / 56% top-3 on OTel-Demo, see
``docs/eval-otel-demo.md``) said candidate *generation* usually reaches the truth
but the *ordering* is wrong: on propagation faults the loud **caller** service
outranks the true upstream **culprit** service it depends on. The learned
ranker scores each service from its own per-service scalars and cannot see that one
anomalous service sits upstream of another — so it ranks by symptom volume.

This module reorders the ranker's candidates using the caller→callee trace graph
(:mod:`src.core.rca.linkage`). It is deliberately **not** the scalar ``tr_calldir``
feature that washed on RE3 LOSO (#155): that used static edge direction alone. The
disambiguator here is **temporal precedence** — a root cause degrades *before* the
symptoms it propagates to. Edge direction is only a weak fallback when timing is
uninformative.

Per candidate service ``s`` (only when ``s`` is itself anomalous), for each other
anomalous service ``n`` connected to ``s`` within ``max_hops`` in the (undirected)
dependency graph:

  * **graph distance** — the contribution decays by ``proximity_decay`` per hop, so
    a direct neighbour weighs more than a transitive one;
  * **symptom magnitude** — a louder ``n`` (more error volume / anomaly) weighs more;
  * **temporal precedence** — if ``s`` degraded before ``n`` (``onset[s] < onset[n]``
    by more than ``min_onset_gap``), ``n`` is a downstream symptom of ``s`` → boost
    ``s``; if ``n`` degraded first, ``s`` is itself downstream → penalise ``s``;
  * **propagation direction** (fallback) — when timing is tied or missing for the
    pair, a small ``direction_weight`` term treats ``s``'s callee (dependency) as the
    more-likely cause, matching the observed caller-shows-symptom pattern.

The adjustment is squashed with ``tanh`` and applied multiplicatively to the base
score, so it *reorders near-ties* rather than overriding a confident ranker:
``new = base * (1 + blend * tanh(adj))``. With an empty graph, no onset data, or a
single candidate the input order is returned unchanged — the reranker is a
best-effort refinement, never a hard gate (opt-in; default off in settings).

Pure logic over primitives (``(service, score)`` + a :class:`ServiceGraph` + onset /
anomaly maps) so it is unit-testable without a database; :func:`rerank_candidates`
is the thin adapter over :class:`~src.core.rca.candidates.RootCauseCandidate`.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Mapping

from src.core.rca.linkage import ServiceGraph

# Frozen hyperparameters (calibrated on RCAEval RE2+RE3 LOSO; see
# scripts/eval_rca_reranker.py and docs/eval-trace-propagation.md). Kept here as
# the single source of truth so the runtime and the eval harness agree.
DEFAULT_BLEND = 0.25
DEFAULT_PROXIMITY_DECAY = 0.5
DEFAULT_MIN_ONSET_GAP = 1.0  # seconds; onset differences below this are "simultaneous"
DEFAULT_DIRECTION_WEIGHT = 0.0  # edge-direction fallback; 0 until the eval earns it
DEFAULT_MAX_HOPS = 2


def _undirected_hops(graph: ServiceGraph, src: str, max_hops: int) -> dict[str, int]:
    """BFS hop distance from ``src`` over edges treated as undirected, out to
    ``max_hops``. Excludes ``src`` itself. Empty when ``src`` is not in the graph."""
    if src not in graph.services:
        return {}
    adj: dict[str, set[str]] = defaultdict(set)
    for a, b in graph.edges:
        adj[a].add(b)
        adj[b].add(a)
    dist: dict[str, int] = {}
    q: deque[tuple[str, int]] = deque([(src, 0)])
    seen = {src}
    while q:
        node, d = q.popleft()
        if d >= max_hops:
            continue
        for nxt in adj.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                dist[nxt] = d + 1
                q.append((nxt, d + 1))
    return dist


def _calls(graph: ServiceGraph, a: str, b: str) -> bool:
    """Does ``a`` call ``b`` directly (``a`` depends on ``b``)?"""
    return (a, b) in graph.edges


def propagation_scores(
    scored: list[tuple[str, float]],
    graph: ServiceGraph,
    onset: Mapping[str, float],
    anomaly: Mapping[str, float],
    *,
    blend: float = DEFAULT_BLEND,
    proximity_decay: float = DEFAULT_PROXIMITY_DECAY,
    min_onset_gap: float = DEFAULT_MIN_ONSET_GAP,
    direction_weight: float = DEFAULT_DIRECTION_WEIGHT,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> list[tuple[str, float]]:
    """Reorder ``(service, base_score)`` by a trace-graph propagation adjustment.

    ``onset`` maps a service to when its anomaly began (any monotonic clock — only
    differences matter; a missing service has no timing). ``anomaly`` maps a service
    to a non-negative magnitude; a service with ``0``/absent magnitude is not treated
    as a symptom or a cause. Returns ``(service, adjusted_score)`` highest first, ties
    broken by service name. Order is unchanged when the graph is empty or fewer than
    two services carry anomaly signal.
    """
    if graph.empty or len(scored) < 2:
        return sorted(scored, key=lambda t: (-t[1], t[0]))

    anomalous = {s for s, _ in scored if anomaly.get(s, 0.0) > 0.0}
    if len(anomalous) < 2:
        return sorted(scored, key=lambda t: (-t[1], t[0]))

    max_mag = max((anomaly.get(s, 0.0) for s in anomalous), default=0.0) or 1.0
    hops_cache: dict[str, dict[str, int]] = {}

    adjusted: list[tuple[str, float]] = []
    for s, base in scored:
        if s not in anomalous:
            adjusted.append((s, base))
            continue
        hops = hops_cache.get(s)
        if hops is None:
            hops = _undirected_hops(graph, s, max_hops)
            hops_cache[s] = hops
        adj = 0.0
        for n, d in hops.items():
            if n not in anomalous:
                continue
            # proximity (graph distance) x symptom magnitude of the neighbour
            w = (proximity_decay ** (d - 1)) * (anomaly.get(n, 0.0) / max_mag)
            os_, on_ = onset.get(s), onset.get(n)
            if os_ is not None and on_ is not None and abs(on_ - os_) > min_onset_gap:
                # temporal precedence: earlier onset => upstream cause
                adj += w if os_ < on_ else -w
            elif direction_weight:
                # timing uninformative: fall back to edge direction. s's callee
                # (s -> n, n is a dependency of s) is the more-likely cause, so
                # being the callee boosts s and being the caller penalises it.
                if _calls(graph, n, s):
                    adj += direction_weight * w
                elif _calls(graph, s, n):
                    adj -= direction_weight * w
        adjusted.append((s, base * (1.0 + blend * math.tanh(adj))))

    adjusted.sort(key=lambda t: (-t[1], t[0]))
    return adjusted


def rerank_candidates(
    candidates,
    graph: ServiceGraph,
    onset: Mapping[str, float],
    anomaly: Mapping[str, float] | None = None,
    **kwargs,
):
    """Reorder :class:`~src.core.rca.candidates.RootCauseCandidate` objects in place
    of their ranker order, using :func:`propagation_scores`.

    ``anomaly`` defaults to each candidate's log-error volume (``features.log_err``)
    — the symptom magnitude the graph propagates. The candidates' ``score`` fields
    are left untouched (they remain the calibrated ranker probabilities); only the
    list order changes, so a downstream calibrator still reads real scores.
    """
    if anomaly is None:
        anomaly = {c.service: float(getattr(c.features, "log_err", 0.0)) for c in candidates}
    by_service = {c.service: c for c in candidates}
    scored = [(c.service, c.score) for c in candidates]
    order = propagation_scores(scored, graph, onset, anomaly, **kwargs)
    return [by_service[s] for s, _ in order]
