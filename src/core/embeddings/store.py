"""Persist log-line and cluster-template embeddings for semantic retrieval.

Ingest writes ``LogEmbedding`` rows after ``LogEntry`` PKs exist.
Clustering upserts ``ClusterEmbedding`` rows keyed by ``(scope, fingerprint)``.
Ask reads log-line vectors; similar-incident search reads cluster vectors.
The stored column is ``Vector(1536)`` (migration 0001); other dimensions are skipped.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.postgresql.dml import Insert
from sqlalchemy.orm import Session

from src.config import get_settings
from src.config.settings import Settings
from src.core.embeddings.provider import EmbeddingsProvider, get_embeddings_provider
from src.db.models import (
    CLUSTER_EMBEDDING_UNIQUE,
    ClusterEmbedding,
    LogEmbedding,
    LogEntry,
)

if TYPE_CHECKING:
    from src.core.clustering.clusterer import ClusterData

log = structlog.get_logger()

# Must match ``log_embeddings.embedding Vector(1536)`` in the applied schema.
STORED_EMBEDDING_DIMS = 1536


def ingest_embeddings_provider(
    with_embeddings: bool,
    settings: Settings | None = None,
) -> EmbeddingsProvider | None:
    """Return a provider to persist ingest embeddings, or ``None`` to skip.

    Skips when the caller did not request embeddings, the provider is
    unavailable, or ``EMBEDDINGS_DIMENSIONS`` is not 1536 (the stored column).
    """
    if not with_embeddings:
        return None
    if settings is None:
        settings = get_settings()
    if settings.embeddings_dimensions != STORED_EMBEDDING_DIMS:
        log.warning(
            "ingest_embeddings_skipped",
            reason="embeddings_dimensions does not match stored Vector(1536)",
            dimensions=settings.embeddings_dimensions,
        )
        return None
    provider = get_embeddings_provider(settings)
    if not provider.is_available():
        log.warning(
            "ingest_embeddings_skipped",
            reason="embeddings provider unavailable",
            provider=settings.embeddings_provider,
        )
        return None
    return provider


def build_embedding_rows(
    entries: list[LogEntry],
    vectors: list[list[float]],
    model_name: str,
    expected_dimensions: int = STORED_EMBEDDING_DIMS,
) -> list[LogEmbedding]:
    """Pair flushed log entries with embedding vectors into ``LogEmbedding`` rows.

    Skips blank messages, missing PKs, and vectors whose length is not
    ``expected_dimensions``. ``entries`` and ``vectors`` must be parallel lists
    over the texts that were actually sent to the embedder.
    """
    if len(entries) != len(vectors):
        raise ValueError(
            f"entries length {len(entries)} does not match vectors length {len(vectors)}"
        )
    rows: list[LogEmbedding] = []
    for entry, vector in zip(entries, vectors):
        if entry.id is None:
            continue
        if len(vector) != expected_dimensions:
            continue
        rows.append(
            LogEmbedding(
                id=uuid.uuid4(),
                log_entry_id=entry.id,
                embedding=vector,
                model_name=model_name,
            )
        )
    return rows


def persist_log_embeddings(
    db: Session,
    entries: list[LogEntry],
    *,
    provider: EmbeddingsProvider | None = None,
    settings: Settings | None = None,
) -> int:
    """Embed a flushed batch of log entries and insert ``LogEmbedding`` rows.

    Fail-open on provider errors and on insert/flush errors: log and return 0
    so ingest still succeeds. Inserts run in a savepoint (``begin_nested``)
    so a failed embedding write cannot roll back the already-flushed
    ``LogEntry`` batch or poison the session.
    Returns the number of rows inserted.
    """
    if not entries:
        return 0
    if settings is None:
        settings = get_settings()
    if settings.embeddings_dimensions != STORED_EMBEDDING_DIMS:
        return 0
    if provider is None:
        provider = get_embeddings_provider(settings)
    if not provider.is_available():
        return 0

    eligible: list[LogEntry] = []
    texts: list[str] = []
    for entry in entries:
        text = (entry.normalized_message or entry.raw_message or "").strip()
        if not text or entry.id is None:
            continue
        eligible.append(entry)
        texts.append(text)
    if not texts:
        return 0

    try:
        vectors = provider.embed_texts(texts)
    except Exception:
        log.warning("embeddings_batch_failed", count=len(texts), exc_info=True)
        return 0

    if len(vectors) != len(eligible):
        log.warning(
            "embeddings_count_mismatch",
            expected=len(eligible),
            got=len(vectors),
        )
        return 0

    rows = build_embedding_rows(
        eligible,
        vectors,
        model_name=settings.embeddings_model,
        expected_dimensions=STORED_EMBEDDING_DIMS,
    )
    if not rows:
        return 0

    try:
        with db.begin_nested():
            db.bulk_save_objects(rows)
            db.flush()
    except Exception:
        log.warning("embeddings_persist_failed", count=len(rows), exc_info=True)
        return 0
    return len(rows)


def cluster_embedding_row_values(
    *,
    scope: str,
    fingerprint: str,
    template: str,
    vector: list[float],
    model_name: str,
    first_seen: Optional[datetime] = None,
    last_seen: Optional[datetime] = None,
    count: int = 0,
    row_id: Optional[uuid.UUID] = None,
    expected_dimensions: int = STORED_EMBEDDING_DIMS,
) -> Optional[dict[str, Any]]:
    """Build an upsert payload for one cluster template. ``None`` if unusable."""
    if not fingerprint or not template.strip():
        return None
    if len(vector) != expected_dimensions:
        return None
    now = datetime.now(tz=timezone.utc)
    return {
        "id": row_id or uuid.uuid4(),
        "scope": scope,
        "fingerprint": fingerprint,
        "template": template,
        "embedding": vector,
        "model_name": model_name,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "count": count,
        "updated_at": now,
    }


def cluster_embedding_upsert_statement(rows: list[dict[str, Any]]) -> Insert:
    """INSERT … ON CONFLICT (scope, fingerprint) DO UPDATE."""
    stmt = pg_insert(ClusterEmbedding).values(rows)
    excluded = stmt.excluded
    return stmt.on_conflict_do_update(
        constraint=CLUSTER_EMBEDDING_UNIQUE,
        set_={
            "template": excluded.template,
            "embedding": excluded.embedding,
            "model_name": excluded.model_name,
            "first_seen": excluded.first_seen,
            "last_seen": excluded.last_seen,
            "count": excluded.count,
            "updated_at": excluded.updated_at,
        },
    )


def persist_cluster_embeddings(
    db: Session,
    clusters: list["ClusterData"],
    *,
    scope: str,
    provider: Optional[EmbeddingsProvider] = None,
    settings: Optional[Settings] = None,
) -> int:
    """Embed cluster templates and upsert ``cluster_embeddings``.

    Fail-open: provider down, dimension mismatch, or write errors log and
    return 0 so the cluster run still succeeds. Writes run in a savepoint.
    """
    if not clusters:
        return 0
    if settings is None:
        settings = get_settings()
    if settings.embeddings_dimensions != STORED_EMBEDDING_DIMS:
        return 0
    if provider is None:
        provider = get_embeddings_provider(settings)
    if not provider.is_available():
        return 0

    eligible: list[Any] = []
    texts: list[str] = []
    for cluster in clusters:
        template = (cluster.representative_message or "").strip()
        if not template or not cluster.fingerprint:
            continue
        eligible.append(cluster)
        texts.append(template)
    if not texts:
        return 0

    try:
        vectors = provider.embed_texts(texts)
    except Exception:
        log.warning("cluster_embeddings_batch_failed", count=len(texts), exc_info=True)
        return 0

    if len(vectors) != len(eligible):
        log.warning(
            "cluster_embeddings_count_mismatch",
            expected=len(eligible),
            got=len(vectors),
        )
        return 0

    rows: list[dict[str, Any]] = []
    for cluster, vector in zip(eligible, vectors):
        payload = cluster_embedding_row_values(
            scope=scope,
            fingerprint=cluster.fingerprint,
            template=cluster.representative_message,
            vector=vector,
            model_name=settings.embeddings_model,
            first_seen=cluster.first_seen,
            last_seen=cluster.last_seen,
            count=cluster.count,
        )
        if payload is not None:
            rows.append(payload)
    if not rows:
        return 0

    try:
        with db.begin_nested():
            db.execute(cluster_embedding_upsert_statement(rows))
            db.flush()
    except Exception:
        log.warning("cluster_embeddings_persist_failed", count=len(rows), exc_info=True)
        return 0
    return len(rows)
