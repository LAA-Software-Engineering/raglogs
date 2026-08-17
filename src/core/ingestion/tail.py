"""Tail-mode ingestion: lifecycle, auto-pause, and worker ticks."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.adapters.base import SourceSpec, TimeWindow
from src.db.models import IngestionJob

log = structlog.get_logger()

TAIL_ADAPTERS: frozenset[str] = frozenset({"cloudwatch", "datadog", "loki"})
TAIL_LIFECYCLE_ACTIONS: frozenset[str] = frozenset({"pause", "resume", "stop"})


class TailLifecycleError(ValueError):
    """Invalid tail lifecycle transition. ``code`` is ``not_tail`` or ``conflict``."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def apply_tail_lifecycle(
    mode: str,
    status: str,
    action: Literal["pause", "resume", "stop"],
) -> str:
    """Return the next status for a tail job.

    Raises ``TailLifecycleError`` when the job is not tail-mode or the
    transition is illegal (resume/pause of a stopped job).
    """
    if mode != "tail":
        raise TailLifecycleError("not_tail", "lifecycle actions require mode=tail")
    if action not in TAIL_LIFECYCLE_ACTIONS:
        raise TailLifecycleError("conflict", f"unknown action {action!r}")

    if action == "stop":
        return "stopped"

    if status == "stopped":
        raise TailLifecycleError(
            "conflict",
            "stopped tail jobs cannot be paused or resumed",
        )

    if action == "pause":
        return "paused"
    return "running"


def consecutive_errors_after_failure(
    current: int,
    threshold: int,
) -> tuple[int, bool]:
    """Return ``(new_count, should_pause)`` after one failed tail tick."""
    new_count = current + 1
    return new_count, new_count >= threshold


def cursors_from_job(job: IngestionJob) -> dict[str, Optional[str]]:
    """Load stream cursors from the dedicated column, falling back to metadata."""
    if job.cursor:
        try:
            data = json.loads(job.cursor)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass
    meta = job.metadata_json or {}
    stored = meta.get("cursors")
    if isinstance(stored, dict):
        return stored
    return {}


def persist_cursors(job: IngestionJob, cursors: dict[str, Optional[str]]) -> None:
    """Write cursors to both ``job.cursor`` (JSON text) and metadata_json."""
    job.cursor = json.dumps(cursors)
    meta = dict(job.metadata_json or {})
    meta["cursors"] = cursors
    job.metadata_json = meta


def has_open_cursors(cursors: dict[str, Optional[str]]) -> bool:
    """True when any stream still has a pagination token (not caught up)."""
    return any(value is not None and str(value) != "" for value in cursors.values())


def is_tail_job_due(job: IngestionJob, now: datetime, poll_interval: int) -> bool:
    """Whether a tail job should be ticked at ``now`` given ``TAIL_POLL_INTERVAL``."""
    if job.mode != "tail" or job.status != "running":
        return False
    if job.last_polled_at is None:
        return True
    return job.last_polled_at <= now - timedelta(seconds=poll_interval)


def _parse_stored_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _held_paging_window(job: IngestionJob) -> Optional[TimeWindow]:
    """Return the stored paging window if an open cursor still needs it."""
    if not has_open_cursors(cursors_from_job(job)):
        return None
    meta = job.metadata_json or {}
    start = _parse_stored_datetime(meta.get("tail_window_start"))
    end = _parse_stored_datetime(meta.get("tail_window_end"))
    if start is None or end is None:
        return None
    return TimeWindow(start=start, end=end)


def tick_window_for_job(
    job: IngestionJob,
    now: datetime,
) -> TimeWindow:
    """Window for one tail poll: last poll → now, else first-window from spec / 1m.

    ``last_polled_at`` is only the poll clock. While any stream still has a
    pagination cursor, reuse ``metadata_json['tail_window_start'|'tail_window_end']``
    — Datadog/CloudWatch page tokens are only valid for that original from/to.
    """
    held = _held_paging_window(job)
    if held is not None:
        return held

    if job.last_polled_at is not None:
        return TimeWindow(start=job.last_polled_at, end=now)

    from src.utils.time import resolve_window

    meta = job.metadata_json or {}
    since = meta.get("since")
    from_raw = meta.get("from_time")
    to_raw = meta.get("to_time")
    if since or from_raw or to_raw:
        from_dt = datetime.fromisoformat(from_raw) if from_raw else None
        to_dt = datetime.fromisoformat(to_raw) if to_raw else None
        start, end = resolve_window(since=since, from_time=from_dt, to_time=to_dt)
        return TimeWindow(start=start, end=end)

    start, end = resolve_window(since="1m")
    return TimeWindow(start=start, end=end)


def _apply_tail_window_progress(
    job: IngestionJob, window: TimeWindow, now: datetime
) -> None:
    """Record poll clock and either hold or release the paging window.

    ``last_polled_at`` is always ``now``. Open cursors persist
    ``tail_window_start`` / ``tail_window_end``; exhausted streams drop them.
    """
    job.last_polled_at = now
    meta = dict(job.metadata_json or {})
    if has_open_cursors(cursors_from_job(job)):
        meta["tail_window_start"] = window.start.isoformat()
        meta["tail_window_end"] = window.end.isoformat()
        job.metadata_json = meta
        return
    meta.pop("tail_window_start", None)
    meta.pop("tail_window_end", None)
    job.metadata_json = meta


def spec_from_tail_job(job: IngestionJob) -> SourceSpec:
    meta = job.metadata_json or {}
    return SourceSpec(
        adapter=str(meta.get("adapter") or job.source_adapter),
        params=dict(meta.get("params") or {}),
        service=meta.get("service"),
        env=meta.get("env"),
    )


def _due_tail_jobs(
    db: Session, now: datetime, poll_interval: int
) -> list[IngestionJob]:
    cutoff = now - timedelta(seconds=poll_interval)
    stmt = (
        select(IngestionJob)
        .where(
            IngestionJob.mode == "tail",
            IngestionJob.status == "running",
            or_(
                IngestionJob.last_polled_at.is_(None),
                IngestionJob.last_polled_at <= cutoff,
            ),
        )
        .with_for_update(skip_locked=True)
    )
    jobs = list(db.execute(stmt).scalars().all())
    # Re-filter in Python so unit tests with mocked execute() still honor
    # last_polled_at / paused status (SQL WHERE is not applied on MagicMock).
    return [job for job in jobs if is_tail_job_due(job, now, poll_interval)]


def tick_one_tail_job(
    db: Session, job: IngestionJob, now: datetime, error_threshold: int
) -> None:
    """Run one adapter poll against an existing tail IngestionJob."""
    from src.core.ingestion.service import ingest_from_source

    meta = job.metadata_json or {}
    spec = spec_from_tail_job(job)
    window = tick_window_for_job(job, now)
    fmt = str(meta.get("format") or "auto")
    source_name = meta.get("source_name")
    with_embeddings = bool(meta.get("with_embeddings") or False)

    try:
        _, _stats = ingest_from_source(
            db=db,
            spec=spec,
            window=window,
            source_name=source_name,
            fmt=fmt,
            resume_cursors=cursors_from_job(job),
            resume_completed_streams=None,
            with_embeddings=with_embeddings,
            existing_job=job,
            finalize=False,
            scope=str(meta.get("scope") or "default"),
        )
    except Exception as exc:
        new_count, should_pause = consecutive_errors_after_failure(
            job.consecutive_errors or 0,
            error_threshold,
        )
        job.consecutive_errors = new_count
        job.error_message = str(exc)
        if should_pause:
            job.status = "paused"
            log.error(
                "tail_job_auto_paused",
                ingestion_job_id=str(job.id),
                consecutive_errors=new_count,
                error=str(exc),
            )
        else:
            log.warning(
                "tail_job_tick_failed",
                ingestion_job_id=str(job.id),
                consecutive_errors=new_count,
                error=str(exc),
            )
        # Stamp last_polled_at so the job is not due again until TAIL_POLL_INTERVAL
        # (otherwise the worker busy-loops and re-reads already-flushed lines).
        job.last_polled_at = now
        db.flush()
        return

    job.consecutive_errors = 0
    job.error_message = None
    _apply_tail_window_progress(job, window, now)
    db.flush()


def tick_tail_jobs(db: Session) -> int:
    """Poll due running tail jobs. Returns the number of jobs ticked."""
    from src.config import get_settings
    from src.core.ingestion.backpressure import (
        ingest_queue_is_full,
        pending_worker_job_count,
    )

    settings = get_settings()
    pending = pending_worker_job_count(db)
    if ingest_queue_is_full(pending, settings.ingest_queue_max):
        log.info("tail_ticks_skipped_queue_full", pending=pending)
        return 0

    now = datetime.now(tz=timezone.utc)
    jobs = _due_tail_jobs(db, now, settings.tail_poll_interval)
    for job in jobs:
        tick_one_tail_job(db, job, now, settings.tail_error_threshold)
    return len(jobs)


def tail_job_counts(db: Session) -> dict[str, Any]:
    """``{running, paused}`` counts for /health."""
    from sqlalchemy import func

    running = db.execute(
        select(func.count())
        .select_from(IngestionJob)
        .where(IngestionJob.mode == "tail", IngestionJob.status == "running")
    ).scalar_one()
    paused = db.execute(
        select(func.count())
        .select_from(IngestionJob)
        .where(IngestionJob.mode == "tail", IngestionJob.status == "paused")
    ).scalar_one()
    return {"running": int(running or 0), "paused": int(paused or 0)}
