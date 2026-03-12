import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import LogEntry, IngestionJob


def get_baseline_counts(
    db: Session,
    baseline_start: datetime,
    baseline_end: datetime,
    service: Optional[str] = None,
    environment: Optional[str] = None,
    exclude_ingestion_job_id: Optional[uuid.UUID] = None,
) -> dict[str, int]:
    """
    Return a dict of {fingerprint: count} for the baseline window.

    When exclude_ingestion_job_id is provided, excludes ALL ingestion jobs that
    started at or after the given job's start time. This ensures the baseline
    only reflects data that pre-dates the current ingestion run.
    """
    q = (
        select(LogEntry.fingerprint, func.count(LogEntry.id).label("cnt"))
        .where(
            LogEntry.timestamp >= baseline_start,
            LogEntry.timestamp <= baseline_end,
            LogEntry.fingerprint.isnot(None),
        )
    )

    if service:
        q = q.where(LogEntry.service == service)
    if environment:
        q = q.where(LogEntry.environment == environment)

    if exclude_ingestion_job_id:
        # Find the start time of the current job
        current_job = db.execute(
            select(IngestionJob).where(IngestionJob.id == exclude_ingestion_job_id)
        ).scalar_one_or_none()

        if current_job and current_job.started_at:
            # Exclude all jobs that started at or after this job (including itself)
            later_jobs = db.execute(
                select(IngestionJob.id).where(
                    IngestionJob.started_at >= current_job.started_at
                )
            ).scalars().all()
            if later_jobs:
                q = q.where(LogEntry.ingestion_job_id.notin_(later_jobs))
        else:
            # Fallback: just exclude the job itself
            q = q.where(LogEntry.ingestion_job_id != exclude_ingestion_job_id)

    q = q.group_by(LogEntry.fingerprint)

    rows = db.execute(q).all()
    return {row.fingerprint: row.cnt for row in rows}


def compute_change_ratio(current_count: int, baseline_count: int) -> float:
    """
    Change ratio with smoothing to avoid division by zero.
    """
    return (current_count + 1) / (baseline_count + 1)
