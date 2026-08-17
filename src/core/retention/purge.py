"""Scheduled / on-demand retention purge (G13).

Deletes expired raw log rows first (``log_embeddings`` and ``cluster_members``
follow ON DELETE CASCADE) while leaving ``cluster_embeddings`` in place so
similar-incident search still works. After the summary TTL, cluster embeddings,
cluster runs, and explanations for that scope are removed.

Unit tests compile the SQL statements without a database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import Select, delete, func, select, union
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Delete

from src.config.settings import Settings
from src.core.retention.policy import (
    RetentionPolicy,
    compute_cutoff,
    resolve_scope_policy,
)
from src.db.models import (
    AppConfig,
    Cluster,
    ClusterEmbedding,
    ClusterRun,
    Explanation,
    LogEmbedding,
    LogEntry,
    ScopeRetention,
    WorkerJob,
)

PURGE_JOB_TYPE = "purge"
LAST_PURGE_AT_KEY = "last_purge_at"

_RAW_EXPIRY = func.coalesce(LogEntry.timestamp, LogEntry.created_at)
_SUMMARY_EMBEDDING_EXPIRY = func.coalesce(
    ClusterEmbedding.last_seen,
    ClusterEmbedding.updated_at,
    ClusterEmbedding.created_at,
)
_CLUSTER_RUN_EXPIRY = func.coalesce(ClusterRun.window_end, ClusterRun.created_at)


@dataclass
class PurgeCounts:
    raw: int = 0
    summary: int = 0
    embedding: int = 0
    scopes: list[str] = field(default_factory=list)

    def add(self, other: "PurgeCounts") -> None:
        self.raw += other.raw
        self.summary += other.summary
        self.embedding += other.embedding
        for scope in other.scopes:
            if scope not in self.scopes:
                self.scopes.append(scope)

    def is_empty(self) -> bool:
        return self.raw == 0 and self.summary == 0 and self.embedding == 0

    def as_dict(self, *, dry_run: bool = False) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "summary": self.summary,
            "embedding": self.embedding,
            "scopes": list(self.scopes),
            "dry_run": dry_run,
        }


def raw_expiry_expression() -> Any:
    """``COALESCE(timestamp, created_at)`` used as the raw-row age."""
    return _RAW_EXPIRY


def summary_embedding_expiry_expression() -> Any:
    """``COALESCE(last_seen, updated_at, created_at)`` for cluster embeddings."""
    return _SUMMARY_EMBEDDING_EXPIRY


def select_expired_log_entry_ids(
    scope: str,
    cutoff: datetime,
    *,
    limit: int,
) -> Select:
    """Ids of raw rows in ``scope`` older than ``cutoff`` (chunked)."""
    return (
        select(LogEntry.id)
        .where(LogEntry.scope == scope)
        .where(_RAW_EXPIRY < cutoff)
        .order_by(_RAW_EXPIRY.asc(), LogEntry.id.asc())
        .limit(limit)
    )


def count_log_embeddings_statement(entry_ids: Iterable[uuid.UUID]) -> Select:
    ids = list(entry_ids)
    return (
        select(func.count())
        .select_from(LogEmbedding)
        .where(LogEmbedding.log_entry_id.in_(ids))
    )


def delete_log_entries_statement(entry_ids: Iterable[uuid.UUID]) -> Delete:
    return delete(LogEntry).where(LogEntry.id.in_(list(entry_ids)))


def select_expired_cluster_embedding_ids(
    scope: str,
    cutoff: datetime,
    *,
    limit: int,
) -> Select:
    return (
        select(ClusterEmbedding.id)
        .where(ClusterEmbedding.scope == scope)
        .where(_SUMMARY_EMBEDDING_EXPIRY < cutoff)
        .order_by(_SUMMARY_EMBEDDING_EXPIRY.asc(), ClusterEmbedding.id.asc())
        .limit(limit)
    )


def delete_cluster_embeddings_statement(embedding_ids: Iterable[uuid.UUID]) -> Delete:
    return delete(ClusterEmbedding).where(ClusterEmbedding.id.in_(list(embedding_ids)))


def select_expired_explanation_ids(
    scope: str,
    cutoff: datetime,
    *,
    limit: int,
) -> Select:
    return (
        select(Explanation.id)
        .where(Explanation.scope == scope)
        .where(Explanation.created_at < cutoff)
        .order_by(Explanation.created_at.asc(), Explanation.id.asc())
        .limit(limit)
    )


def delete_explanations_statement(explanation_ids: Iterable[uuid.UUID]) -> Delete:
    return delete(Explanation).where(Explanation.id.in_(list(explanation_ids)))


def select_expired_cluster_run_ids(
    scope: str,
    cutoff: datetime,
    *,
    limit: int,
) -> Select:
    return (
        select(ClusterRun.id)
        .where(ClusterRun.scope == scope)
        .where(_CLUSTER_RUN_EXPIRY < cutoff)
        .order_by(_CLUSTER_RUN_EXPIRY.asc(), ClusterRun.id.asc())
        .limit(limit)
    )


def count_clusters_for_runs_statement(run_ids: Iterable[uuid.UUID]) -> Select:
    ids = list(run_ids)
    return (
        select(func.count()).select_from(Cluster).where(Cluster.cluster_run_id.in_(ids))
    )


def delete_cluster_runs_statement(run_ids: Iterable[uuid.UUID]) -> Delete:
    return delete(ClusterRun).where(ClusterRun.id.in_(list(run_ids)))


def scopes_to_purge_statement() -> Select:
    """Distinct scopes that have data or a retention override."""
    return union(
        select(LogEntry.scope.label("scope")).where(LogEntry.scope.isnot(None)),
        select(ClusterEmbedding.scope.label("scope")).where(
            ClusterEmbedding.scope.isnot(None)
        ),
        select(ClusterRun.scope.label("scope")).where(ClusterRun.scope.isnot(None)),
        select(Explanation.scope.label("scope")).where(Explanation.scope.isnot(None)),
        select(ScopeRetention.scope.label("scope")),
    )


def active_purge_job_statement() -> Select:
    return (
        select(WorkerJob.id)
        .where(WorkerJob.job_type == PURGE_JOB_TYPE)
        .where(WorkerJob.status.in_(("pending", "running")))
        .limit(1)
    )


def should_enqueue_purge(
    *,
    last_purge_at: Optional[datetime],
    has_active_purge: bool,
    now: datetime,
    interval_seconds: int,
) -> bool:
    """Idle-poll decision: enqueue when due and no purge is already claimed."""
    if has_active_purge:
        return False
    if interval_seconds <= 0:
        return False
    if last_purge_at is None:
        return True
    stamp = last_purge_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    clock = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return (clock - stamp).total_seconds() >= interval_seconds


def read_last_purge_at(db: Session) -> Optional[datetime]:
    row = db.get(AppConfig, LAST_PURGE_AT_KEY)
    if row is None:
        return None
    raw = row.value_json
    if isinstance(raw, dict):
        raw = raw.get("at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def write_last_purge_at(db: Session, now: datetime) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    clock = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    iso = clock.isoformat()
    stmt = pg_insert(AppConfig).values(key=LAST_PURGE_AT_KEY, value_json=iso)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"value_json": stmt.excluded.value_json},
    )
    db.execute(stmt)


def _record(kind: str, count: int, *, dry_run: bool) -> None:
    if dry_run or count <= 0:
        return
    from src.observability.metrics import record_purge_rows

    record_purge_rows(count, kind=kind)


def _scalar_ids(rows: Iterable[Any]) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for row in rows:
        value = row[0] if not hasattr(row, "id") else getattr(row, "id", row[0])
        if value is None:
            continue
        out.append(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
    return out


def _count_result(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def purge_raw_chunk(
    db: Session,
    scope: str,
    cutoff: datetime,
    *,
    limit: int,
    dry_run: bool = False,
) -> PurgeCounts:
    """Delete up to ``limit`` expired log rows in ``scope``. Embeddings CASCADE."""
    rows = db.execute(select_expired_log_entry_ids(scope, cutoff, limit=limit)).all()
    ids = _scalar_ids(rows)
    counts = PurgeCounts()
    if not ids:
        return counts
    embedding_n = _count_result(
        db.execute(count_log_embeddings_statement(ids)).scalar_one()
    )
    if not dry_run:
        db.execute(delete_log_entries_statement(ids))
    counts.raw = len(ids)
    counts.embedding = embedding_n
    _record("raw", counts.raw, dry_run=dry_run)
    _record("embedding", counts.embedding, dry_run=dry_run)
    return counts


def purge_summary_chunk(
    db: Session,
    scope: str,
    cutoff: datetime,
    *,
    limit: int,
    dry_run: bool = False,
) -> PurgeCounts:
    """Delete expired cluster embeddings / runs / explanations in ``scope``."""
    counts = PurgeCounts()

    embedding_rows = db.execute(
        select_expired_cluster_embedding_ids(scope, cutoff, limit=limit)
    ).all()
    embedding_ids = _scalar_ids(embedding_rows)
    if embedding_ids:
        if not dry_run:
            db.execute(delete_cluster_embeddings_statement(embedding_ids))
        counts.embedding += len(embedding_ids)
        _record("embedding", len(embedding_ids), dry_run=dry_run)

    explanation_rows = db.execute(
        select_expired_explanation_ids(scope, cutoff, limit=limit)
    ).all()
    explanation_ids = _scalar_ids(explanation_rows)
    if explanation_ids:
        if not dry_run:
            db.execute(delete_explanations_statement(explanation_ids))
        counts.summary += len(explanation_ids)
        _record("summary", len(explanation_ids), dry_run=dry_run)

    run_rows = db.execute(
        select_expired_cluster_run_ids(scope, cutoff, limit=limit)
    ).all()
    run_ids = _scalar_ids(run_rows)
    if run_ids:
        cluster_n = _count_result(
            db.execute(count_clusters_for_runs_statement(run_ids)).scalar_one()
        )
        if not dry_run:
            db.execute(delete_cluster_runs_statement(run_ids))
        summary_n = len(run_ids) + cluster_n
        counts.summary += summary_n
        _record("summary", summary_n, dry_run=dry_run)

    return counts


def purge_scope_batch(
    db: Session,
    policy: RetentionPolicy,
    *,
    now: datetime,
    limit: int,
    dry_run: bool = False,
) -> PurgeCounts:
    """One raw chunk then one summary chunk for a single scope (G8)."""
    counts = PurgeCounts(scopes=[policy.scope])
    raw_cutoff = compute_cutoff(policy.raw_interval, now=now)
    if raw_cutoff is not None:
        counts.add(
            purge_raw_chunk(db, policy.scope, raw_cutoff, limit=limit, dry_run=dry_run)
        )
    summary_cutoff = compute_cutoff(policy.summary_interval, now=now)
    if summary_cutoff is not None:
        counts.add(
            purge_summary_chunk(
                db, policy.scope, summary_cutoff, limit=limit, dry_run=dry_run
            )
        )
    return counts


def list_scopes_to_purge(db: Session) -> list[str]:
    rows = db.execute(scopes_to_purge_statement()).all()
    scopes: list[str] = []
    seen: set[str] = set()
    for row in rows:
        value = row[0] if not isinstance(row, str) else row
        if not value:
            continue
        scope = str(value)
        if scope in seen:
            continue
        seen.add(scope)
        scopes.append(scope)
    scopes.sort()
    return scopes


def run_purge(
    db: Session,
    *,
    scope: Optional[str] = None,
    dry_run: bool = False,
    max_chunks: Optional[int] = 1,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> PurgeCounts:
    """Purge expired rows. Worker uses ``max_chunks=1``; CLI drains all chunks."""
    from src.config import get_settings

    cfg = settings or get_settings()
    clock = now or datetime.now(tz=timezone.utc)
    limit = max(1, int(cfg.purge_chunk_size))
    scopes = [scope] if scope else list_scopes_to_purge(db)
    totals = PurgeCounts()
    for item in scopes:
        policy = resolve_scope_policy(db, item, settings=cfg)
        chunks = 0
        while True:
            batch = purge_scope_batch(
                db, policy, now=clock, limit=limit, dry_run=dry_run
            )
            totals.add(batch)
            chunks += 1
            if batch.raw == 0 and batch.summary == 0 and batch.embedding == 0:
                if item not in totals.scopes:
                    totals.scopes.append(item)
                break
            if max_chunks is not None and chunks >= max_chunks:
                break
    if not dry_run:
        write_last_purge_at(db, clock)
    return totals


def run_purge_job(db: Session, worker_job: Any) -> dict[str, Any]:
    """Execute a claimed ``purge`` worker job (one chunk per scope)."""
    payload = getattr(worker_job, "payload_json", None) or {}
    scope = payload.get("scope") if isinstance(payload, dict) else None
    scope_str = str(scope).strip() if isinstance(scope, str) and scope.strip() else None
    counts = run_purge(db, scope=scope_str, dry_run=False, max_chunks=1)
    return counts.as_dict(dry_run=False)


def maybe_enqueue_purge(
    db: Session,
    *,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> bool:
    """Enqueue a purge job on idle poll when the interval has elapsed.

    Safe under SKIP LOCKED: duplicate pending rows are idempotent. Does not
    enqueue when a purge job is already pending or running.
    """
    from src.config import get_settings

    cfg = settings or get_settings()
    clock = now or datetime.now(tz=timezone.utc)
    active = db.execute(active_purge_job_statement()).scalar_one_or_none()
    last = read_last_purge_at(db)
    if not should_enqueue_purge(
        last_purge_at=last,
        has_active_purge=active is not None,
        now=clock,
        interval_seconds=cfg.purge_interval_seconds,
    ):
        return False
    job = WorkerJob(
        job_type=PURGE_JOB_TYPE,
        status="pending",
        payload_json={},
    )
    db.add(job)
    db.flush()
    return True
