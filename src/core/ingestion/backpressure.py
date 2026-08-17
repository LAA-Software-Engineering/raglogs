"""Ingest worker-queue backpressure (G9).

When pending WorkerJobs >= INGEST_QUEUE_MAX, POST /ingestions and
POST /ingestions/lines return 429 INGEST_QUEUE_FULL. Tail ticks skip
the same ceiling. Distinct from API RATE_LIMITED (token bucket).
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import WorkerJob


def pending_worker_job_count(db: Session) -> int:
    """Return the number of WorkerJobs waiting to be claimed."""
    count = db.execute(
        select(func.count()).select_from(WorkerJob).where(WorkerJob.status == "pending")
    ).scalar_one()
    return int(count or 0)


def ingest_queue_is_full(pending_count: int, max_depth: int) -> bool:
    """True when enqueue/push should reject with 429."""
    return pending_count >= max_depth
