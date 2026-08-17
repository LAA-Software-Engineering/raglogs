"""Cross-incident similar-fingerprint search (G11 / POST /v1/query/similar).

Semantic path: pgvector cosine NN over ``cluster_embeddings``.
Fingerprint fallback: exact fingerprint match when the embeddings provider
is down, vectors are missing, or ANN returns nothing. Never raises to the
caller — empty matches with ``retrieval_mode="fingerprint"`` on failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.config.settings import Settings
from src.core.embeddings.provider import EmbeddingsProvider, get_embeddings_provider
from src.core.embeddings.store import STORED_EMBEDDING_DIMS
from src.db.models import DEFAULT_LOG_SCOPE, ClusterEmbedding, LogEntry

log = structlog.get_logger()

RETRIEVAL_SEMANTIC = "semantic"
RETRIEVAL_FINGERPRINT = "fingerprint"
FINGERPRINT_MATCH_SIMILARITY = 1.0


@dataclass(frozen=True)
class SimilarVisibility:
    """Which scopes a caller may see in similar-incident results."""

    cross_scope: bool
    # None = every scope is visible; otherwise SQL is pinned to this scope.
    visible_scope: Optional[str]


@dataclass
class QueryCluster:
    fingerprint: str
    template: str = ""


@dataclass
class SimilarMatch:
    scope: str
    fingerprint: str
    template: Optional[str]
    similarity: float
    first_seen: Optional[datetime]
    last_seen: Optional[datetime]
    count: int


@dataclass
class SimilarResult:
    query_clusters: list[QueryCluster]
    matches: list[SimilarMatch]
    retrieval_mode: str
    window_start: datetime
    window_end: datetime


def resolve_similar_visibility(
    *,
    auth_enabled: bool,
    resolved_scope: str,
    cross_scope_requested: Optional[bool],
    role: str = "",
    allow_scope_override: bool = False,
) -> SimilarVisibility:
    """Decide cross-scope vs same-scope for ``/similar``.

    * ``AUTH_ENABLED=false``: cross-scope allowed (default on).
    * ``admin`` role: cross-scope by default; ``cross_scope=false`` pins.
    * ``query`` with ``allow_scope_override``: cross-scope only when
      ``cross_scope=true``.
    * Pinned ``query`` (and OIDC): same-scope only, even if requested.
    """
    if not auth_enabled:
        cross = True if cross_scope_requested is None else bool(cross_scope_requested)
        return SimilarVisibility(
            cross_scope=cross,
            visible_scope=None if cross else resolved_scope,
        )

    if role == "admin":
        cross = True if cross_scope_requested is None else bool(cross_scope_requested)
        return SimilarVisibility(
            cross_scope=cross,
            visible_scope=None if cross else resolved_scope,
        )

    if allow_scope_override:
        cross = bool(cross_scope_requested)
        return SimilarVisibility(
            cross_scope=cross,
            visible_scope=None if cross else resolved_scope,
        )

    return SimilarVisibility(cross_scope=False, visible_scope=resolved_scope)


def collect_query_fingerprints(
    fingerprint: Optional[str],
    fingerprints: Optional[list[str]],
) -> list[str]:
    """Merge ``fingerprint`` / ``fingerprints`` and drop blanks, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for value in [fingerprint, *(fingerprints or [])]:
        if not value:
            continue
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        out.append(stripped)
    return out


def query_clusters_from_fingerprints(
    fingerprints: list[str],
    templates: Optional[dict[str, str]] = None,
) -> list[QueryCluster]:
    """Build query clusters without re-clustering. Missing templates stay empty."""
    lookup = templates or {}
    return [
        QueryCluster(fingerprint=fp, template=lookup.get(fp, "")) for fp in fingerprints
    ]


def query_clusters_from_cluster_data(
    clusters: list[object], *, limit: int = 5
) -> list[QueryCluster]:
    """Take the top clustered fingerprints (already importance-sorted) as the query."""
    out: list[QueryCluster] = []
    for cluster in clusters[:limit]:
        fp = str(getattr(cluster, "fingerprint", "") or "")
        if not fp:
            continue
        template = str(getattr(cluster, "representative_message", "") or "")
        out.append(QueryCluster(fingerprint=fp, template=template))
    return out


def render_similar_summary(matches: list[SimilarMatch], retrieval_mode: str) -> str:
    """Short rules-only prose for ``rendered_text``."""
    if not matches:
        if retrieval_mode == RETRIEVAL_FINGERPRINT:
            return "No similar incidents found (fingerprint match)."
        return "No similar incidents found."
    first = matches[0]
    extra = f" and {len(matches) - 1} more" if len(matches) > 1 else ""
    return (
        f"Saw fingerprint {first.fingerprint} in {first.scope} "
        f"(similarity {first.similarity:.2f}){extra}."
    )


def apply_visibility_filters(
    stmt: Select,
    *,
    query_scope: str,
    query_fingerprints: list[str],
    visibility: SimilarVisibility,
    scope_column: object,
    fingerprint_column: object,
) -> Select:
    """Restrict a select to scopes the caller can see; drop the query pairs."""
    if visibility.cross_scope:
        if visibility.visible_scope is not None:
            stmt = stmt.where(scope_column == visibility.visible_scope)
        stmt = stmt.where(scope_column != query_scope)
    else:
        pinned = visibility.visible_scope or query_scope
        stmt = stmt.where(scope_column == pinned)
        if query_fingerprints:
            stmt = stmt.where(fingerprint_column.notin_(query_fingerprints))
    return stmt


def cluster_embedding_semantic_statement(
    query_vector: list[float],
    *,
    query_scope: str,
    query_fingerprints: list[str],
    visibility: SimilarVisibility,
    min_similarity: float,
    limit: int,
) -> Select:
    """Compile-able ANN select over ``cluster_embeddings`` (cosine distance)."""
    distance = ClusterEmbedding.embedding.cosine_distance(query_vector)
    similarity = 1 - distance
    stmt = select(
        ClusterEmbedding.scope,
        ClusterEmbedding.fingerprint,
        ClusterEmbedding.template,
        ClusterEmbedding.first_seen,
        ClusterEmbedding.last_seen,
        ClusterEmbedding.count,
        similarity.label("similarity"),
    ).where(similarity >= min_similarity)
    stmt = apply_visibility_filters(
        stmt,
        query_scope=query_scope,
        query_fingerprints=query_fingerprints,
        visibility=visibility,
        scope_column=ClusterEmbedding.scope,
        fingerprint_column=ClusterEmbedding.fingerprint,
    )
    return stmt.order_by(distance.asc()).limit(limit)


def cluster_embedding_fingerprint_statement(
    fingerprints: list[str],
    *,
    query_scope: str,
    visibility: SimilarVisibility,
    limit: int,
) -> Select:
    """Exact fingerprint match on ``cluster_embeddings``."""
    stmt = select(
        ClusterEmbedding.scope,
        ClusterEmbedding.fingerprint,
        ClusterEmbedding.template,
        ClusterEmbedding.first_seen,
        ClusterEmbedding.last_seen,
        ClusterEmbedding.count,
    ).where(ClusterEmbedding.fingerprint.in_(fingerprints))
    stmt = apply_visibility_filters(
        stmt,
        query_scope=query_scope,
        query_fingerprints=fingerprints,
        visibility=visibility,
        scope_column=ClusterEmbedding.scope,
        fingerprint_column=ClusterEmbedding.fingerprint,
    )
    return stmt.limit(limit)


def log_entry_fingerprint_statement(
    fingerprints: list[str],
    *,
    query_scope: str,
    visibility: SimilarVisibility,
    limit: int,
) -> Select:
    """Fingerprint-equality fallback over ``log_entries`` when no cluster vectors exist."""
    stmt = (
        select(
            LogEntry.scope,
            LogEntry.fingerprint,
            func.min(LogEntry.normalized_message).label("template"),
            func.min(LogEntry.timestamp).label("first_seen"),
            func.max(LogEntry.timestamp).label("last_seen"),
            func.count().label("count"),
        )
        .where(LogEntry.fingerprint.in_(fingerprints))
        .where(LogEntry.fingerprint.isnot(None))
        .group_by(LogEntry.scope, LogEntry.fingerprint)
    )
    stmt = apply_visibility_filters(
        stmt,
        query_scope=query_scope,
        query_fingerprints=fingerprints,
        visibility=visibility,
        scope_column=LogEntry.scope,
        fingerprint_column=LogEntry.fingerprint,
    )
    return stmt.limit(limit)


def _row_to_match(row: object, similarity: float) -> Optional[SimilarMatch]:
    scope = getattr(row, "scope", None)
    fingerprint = getattr(row, "fingerprint", None)
    if not scope or not fingerprint:
        return None
    return SimilarMatch(
        scope=str(scope),
        fingerprint=str(fingerprint),
        template=getattr(row, "template", None),
        similarity=float(similarity),
        first_seen=getattr(row, "first_seen", None),
        last_seen=getattr(row, "last_seen", None),
        count=int(getattr(row, "count", 0) or 0),
    )


def _merge_matches(
    existing: dict[tuple[str, str], SimilarMatch], match: SimilarMatch
) -> None:
    key = (match.scope, match.fingerprint)
    previous = existing.get(key)
    if previous is None or match.similarity > previous.similarity:
        existing[key] = match


def _ranked(
    matches: dict[tuple[str, str], SimilarMatch], limit: int
) -> list[SimilarMatch]:
    ranked = sorted(matches.values(), key=lambda m: m.similarity, reverse=True)
    return ranked[:limit]


def search_similar_semantic(
    db: Session,
    query_clusters: list[QueryCluster],
    *,
    query_scope: str,
    visibility: SimilarVisibility,
    provider: EmbeddingsProvider,
    min_similarity: float,
    limit: int,
) -> list[SimilarMatch]:
    """Embed query templates and ANN-search ``cluster_embeddings``."""
    texts = [c.template.strip() or c.fingerprint for c in query_clusters]
    vectors = provider.embed_texts(texts)
    if len(vectors) != len(query_clusters):
        return []
    fingerprints = [c.fingerprint for c in query_clusters]
    merged: dict[tuple[str, str], SimilarMatch] = {}
    for vector in vectors:
        if len(vector) != STORED_EMBEDDING_DIMS:
            continue
        stmt = cluster_embedding_semantic_statement(
            vector,
            query_scope=query_scope,
            query_fingerprints=fingerprints,
            visibility=visibility,
            min_similarity=min_similarity,
            limit=limit,
        )
        for row in db.execute(stmt).all():
            similarity = float(getattr(row, "similarity", 0.0) or 0.0)
            match = _row_to_match(row, similarity)
            if match is not None:
                _merge_matches(merged, match)
    return _ranked(merged, limit)


def search_similar_fingerprint(
    db: Session,
    query_clusters: list[QueryCluster],
    *,
    query_scope: str,
    visibility: SimilarVisibility,
    limit: int,
) -> list[SimilarMatch]:
    """Exact fingerprint match on cluster embeddings, then log entries."""
    fingerprints = [c.fingerprint for c in query_clusters if c.fingerprint]
    if not fingerprints:
        return []
    merged: dict[tuple[str, str], SimilarMatch] = {}
    stmt = cluster_embedding_fingerprint_statement(
        fingerprints,
        query_scope=query_scope,
        visibility=visibility,
        limit=limit,
    )
    for row in db.execute(stmt).all():
        match = _row_to_match(row, FINGERPRINT_MATCH_SIMILARITY)
        if match is not None:
            _merge_matches(merged, match)
    if merged:
        return _ranked(merged, limit)

    fallback = log_entry_fingerprint_statement(
        fingerprints,
        query_scope=query_scope,
        visibility=visibility,
        limit=limit,
    )
    for row in db.execute(fallback).all():
        match = _row_to_match(row, FINGERPRINT_MATCH_SIMILARITY)
        if match is not None:
            _merge_matches(merged, match)
    return _ranked(merged, limit)


def find_similar_incidents(
    db: Session,
    query_clusters: list[QueryCluster],
    *,
    query_scope: str = DEFAULT_LOG_SCOPE,
    visibility: Optional[SimilarVisibility] = None,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    top: int = 10,
    provider: Optional[EmbeddingsProvider] = None,
    settings: Optional[Settings] = None,
) -> SimilarResult:
    """Return nearby historical fingerprints. Never raises."""
    now = datetime.now(tz=timezone.utc)
    start = window_start or now
    end = window_end or now
    vis = visibility or SimilarVisibility(cross_scope=False, visible_scope=query_scope)
    empty = SimilarResult(
        query_clusters=list(query_clusters),
        matches=[],
        retrieval_mode=RETRIEVAL_FINGERPRINT,
        window_start=start,
        window_end=end,
    )
    if not query_clusters:
        return empty

    if settings is None:
        settings = get_settings()
    if provider is None:
        provider = get_embeddings_provider(settings)

    limit = max(1, top)
    try:
        if (
            provider.is_available()
            and settings.embeddings_dimensions == STORED_EMBEDDING_DIMS
        ):
            matches = search_similar_semantic(
                db,
                query_clusters,
                query_scope=query_scope,
                visibility=vis,
                provider=provider,
                min_similarity=settings.similar_semantic_min_similarity,
                limit=limit,
            )
            if matches:
                return SimilarResult(
                    query_clusters=list(query_clusters),
                    matches=matches,
                    retrieval_mode=RETRIEVAL_SEMANTIC,
                    window_start=start,
                    window_end=end,
                )
    except Exception:
        log.warning("similar_semantic_failed", exc_info=True)

    try:
        matches = search_similar_fingerprint(
            db,
            query_clusters,
            query_scope=query_scope,
            visibility=vis,
            limit=limit,
        )
        return SimilarResult(
            query_clusters=list(query_clusters),
            matches=matches,
            retrieval_mode=RETRIEVAL_FINGERPRINT,
            window_start=start,
            window_end=end,
        )
    except Exception:
        log.warning("similar_fingerprint_failed", exc_info=True)
        return empty
