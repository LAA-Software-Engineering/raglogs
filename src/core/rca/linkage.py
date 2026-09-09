"""Service dependency linkage from traces (#82 T1).

Trigger detection must not offer a change in service X as the cause of errors
confined to an unrelated service Y. ``trace_id`` / ``parent_span_id`` are ingested
(#126) but unused in explain; this module turns them into a caller→callee service
graph and a linkage test.

Pure graph logic (``ServiceGraph``) over already-fetched span records, plus a thin
:func:`build_service_graph` DB layer. When a scope has no traces the graph is empty
and callers fall back to same-service overlap (graceful, like the ranker) — linkage
is *skipped, not failed*.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import TraceSpan


@dataclass
class ServiceGraph:
    """Directed caller→callee service graph. An edge ``(A, B)`` means a span in A
    is the parent of a span in B — i.e. A calls B, so A depends on B."""

    edges: set[tuple[str, str]] = field(default_factory=set)
    services: set[str] = field(default_factory=set)
    _adj_cache: Optional[dict[str, set[str]]] = field(
        default=None, init=False, compare=False, repr=False
    )

    @property
    def empty(self) -> bool:
        return not self.services

    def _adj(self) -> dict[str, set[str]]:
        # Built once and cached; the graph is populated during construction and
        # only queried (read-only) afterwards, so linked()'s two reachable()
        # calls share one adjacency map instead of rebuilding it each time.
        if self._adj_cache is None:
            adj: dict[str, set[str]] = defaultdict(set)
            for a, b in self.edges:
                adj[a].add(b)
            self._adj_cache = adj
        return self._adj_cache

    def reachable(self, src: str, dst: str) -> bool:
        """Is ``dst`` reachable from ``src`` following caller→callee edges
        (``src`` (transitively) depends on ``dst``)?"""
        if src == dst:
            return True
        adj = self._adj()
        seen = {src}
        q: deque[str] = deque([src])
        while q:
            node = q.popleft()
            for nxt in adj.get(node, ()):
                if nxt == dst:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        return False

    def linked(self, a: str, b: str) -> bool:
        """Are ``a`` and ``b`` related — same service, or connected either
        direction in the dependency graph? A change in one can plausibly affect
        the other. Unknown services (absent from any trace) are treated as
        unlinked so the caller can fall back to same-service overlap."""
        if a == b:
            return True
        if a not in self.services or b not in self.services:
            return False
        return self.reachable(a, b) or self.reachable(b, a)


def build_service_graph(
    db: Session,
    scope: str,
    window_start: datetime,
    window_end: datetime,
) -> ServiceGraph:
    """Build the caller→callee service graph from ``trace_spans`` in the window."""
    rows = db.execute(
        select(TraceSpan.span_id, TraceSpan.parent_span_id, TraceSpan.service).where(
            TraceSpan.scope == scope,
            TraceSpan.start_time >= window_start,
            TraceSpan.start_time <= window_end,
        )
    ).all()
    return service_graph_from_spans(rows)


def service_graph_from_spans(spans) -> ServiceGraph:
    """Pure graph builder over span rows/objects exposing ``span_id`` /
    ``parent_span_id`` / ``service`` (tuples or attribute objects)."""
    svc_of: dict[str, str] = {}
    parent_of: dict[str, Optional[str]] = {}
    for sp in spans:
        span_id, parent_span_id, service = _span_fields(sp)
        if span_id is None or not service:
            continue
        svc_of[span_id] = service
        parent_of[span_id] = parent_span_id

    graph = ServiceGraph()
    graph.services.update(svc_of.values())
    for span_id, parent_span_id in parent_of.items():
        if parent_span_id is None:
            continue
        parent_service = svc_of.get(parent_span_id)
        child_service = svc_of.get(span_id)
        if parent_service and child_service and parent_service != child_service:
            graph.edges.add((parent_service, child_service))
    return graph


def _span_fields(sp):
    if isinstance(sp, (tuple, list)):
        return sp[0], sp[1], sp[2]
    return (
        getattr(sp, "span_id", None),
        getattr(sp, "parent_span_id", None),
        getattr(sp, "service", None),
    )
