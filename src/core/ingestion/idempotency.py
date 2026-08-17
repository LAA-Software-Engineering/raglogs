"""Request-level ingest idempotency (Idempotency-Key on POST /v1/ingestions)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog
from sqlalchemy.exc import IntegrityError

from src.db.models import IngestIdempotencyKey

log = structlog.get_logger()

MAX_IDEMPOTENCY_KEY_LENGTH = 256


class InvalidIdempotencyKey(ValueError):
    """Empty or oversized Idempotency-Key."""


def parse_idempotency_key(raw: Optional[str]) -> Optional[str]:
    """Return a stored key, or None when the header is omitted.

    Raises ``InvalidIdempotencyKey`` for blank or oversized values.
    """
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        raise InvalidIdempotencyKey("Idempotency-Key must be a non-empty string")
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise InvalidIdempotencyKey("Idempotency-Key exceeds 256 characters")
    return key


def key_log_prefix(key: str) -> str:
    """Short prefix safe to log — never the full key."""
    if len(key) <= 8:
        return key[:2] + "…" if len(key) > 2 else "…"
    return key[:8]


def get_idempotency_row(db: Any, key: str) -> Optional[IngestIdempotencyKey]:
    return (
        db.query(IngestIdempotencyKey).filter(IngestIdempotencyKey.key == key).first()
    )


def is_active(row: IngestIdempotencyKey, now: datetime) -> bool:
    expires = row.expires_at
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    compare = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return expires > compare


def lookup_active_key(
    db: Any, key: str, now: datetime
) -> Optional[IngestIdempotencyKey]:
    row = get_idempotency_row(db, key)
    if row is None or not is_active(row, now):
        return None
    return row


def store_idempotency_key(
    db: Any,
    key: str,
    *,
    worker_job_id: Optional[uuid.UUID],
    ingestion_job_id: Optional[uuid.UUID],
    mode: str,
    now: datetime,
    ttl_seconds: int,
) -> tuple[IngestIdempotencyKey, bool]:
    """Insert key → job mapping. Returns ``(row, is_replay)``.

    Expired rows are deleted then replaced. A unique-key race returns the
    winner's row as a replay.
    """
    existing = get_idempotency_row(db, key)
    if existing is not None:
        if is_active(existing, now):
            return existing, True
        db.delete(existing)
        db.flush()

    row = IngestIdempotencyKey(
        key=key,
        worker_job_id=worker_job_id,
        ingestion_job_id=ingestion_job_id,
        mode=mode,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row, False
    except IntegrityError:
        winner = get_idempotency_row(db, key)
        if winner is not None and is_active(winner, now):
            log.info("ingest_idempotency_race", key_prefix=key_log_prefix(key))
            return winner, True
        raise
