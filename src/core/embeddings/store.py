"""Persist log-line embeddings for semantic retrieval.

Ingest writes ``LogEmbedding`` rows after ``LogEntry`` PKs exist.
Ask reads those vectors via pgvector cosine similarity. The stored
column is ``Vector(1536)`` (migration 0001); other dimensions are skipped.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.orm import Session

from src.config import get_settings
from src.config.settings import Settings
from src.core.embeddings.provider import EmbeddingsProvider, get_embeddings_provider
from src.db.models import LogEmbedding, LogEntry

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
