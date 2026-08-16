"""
raglogs background worker.

Polls the worker_jobs table for pending jobs and executes them.
No external queue dependency — uses PostgreSQL as the queue.
Safe to run multiple workers (uses SELECT FOR UPDATE SKIP LOCKED).
"""
import signal
import time
import uuid
from datetime import datetime, timezone

import structlog

log = structlog.get_logger()

POLL_INTERVAL = 2  # seconds between polls when idle
BATCH_SIZE = 1     # one job at a time per worker process


def claim_next_job(db):
    """Atomically claim one pending worker job. Returns job or None."""
    from sqlalchemy import select, update
    from src.db.models import WorkerJob

    # SELECT FOR UPDATE SKIP LOCKED ensures multiple workers don't double-claim
    stmt = (
        select(WorkerJob)
        .where(WorkerJob.status == "pending")
        .order_by(WorkerJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = db.execute(stmt).scalar_one_or_none()
    if job is None:
        return None

    job.status = "running"
    job.started_at = datetime.now(tz=timezone.utc)
    db.flush()
    return job


def run_ingest_job(db, worker_job) -> dict:
    """Execute an ingest worker job. Returns result dict."""
    payload = worker_job.payload_json
    adapter = payload.get("adapter", "file")

    if adapter == "file":
        from src.core.ingestion.service import ingest_files

        job, stats = ingest_files(
            db=db,
            paths=payload["paths"],
            recursive=payload.get("recursive", False),
            source_name=payload.get("source_name"),
            default_service=payload.get("service"),
            default_env=payload.get("env"),
            fmt=payload.get("format", "auto"),
        )
    else:
        import uuid
        from datetime import datetime

        from src.adapters.base import SourceSpec, TimeWindow
        from src.core.ingestion.service import ingest_from_source
        from src.db.models import IngestionJob

        window = None
        if payload.get("window_start") and payload.get("window_end"):
            window = TimeWindow(
                start=datetime.fromisoformat(payload["window_start"]),
                end=datetime.fromisoformat(payload["window_end"]),
            )

        resume_cursors = None
        if payload.get("resume_ingestion_job_id"):
            prior = db.query(IngestionJob).filter(
                IngestionJob.id == uuid.UUID(payload["resume_ingestion_job_id"])
            ).first()
            if prior is not None:
                resume_cursors = (prior.metadata_json or {}).get("cursors")

        spec = SourceSpec(
            adapter=adapter,
            params=payload.get("params", {}),
            service=payload.get("service"),
            env=payload.get("env"),
        )
        job, stats = ingest_from_source(
            db=db,
            spec=spec,
            window=window,
            source_name=payload.get("source_name"),
            fmt=payload.get("format", "auto"),
            resume_cursors=resume_cursors,
        )

    # Link worker job → ingestion job
    worker_job.ingestion_job_id = job.id

    return {
        "ingestion_job_id": str(job.id),
        "files_processed": stats.files_processed,
        "lines_read": stats.lines_read,
        "parsed_count": stats.parsed_count,
        "error_count": stats.error_count,
        "services_detected": list(stats.services_detected),
        "duration_seconds": stats.duration_seconds,
    }


def process_one(db) -> bool:
    """Claim and execute one job. Returns True if a job was processed."""
    from src.db.models import WorkerJob

    worker_job = claim_next_job(db)
    if worker_job is None:
        return False

    job_log = log.bind(worker_job_id=str(worker_job.id), job_type=worker_job.job_type)
    job_log.info("worker_job_started")

    try:
        if worker_job.job_type == "ingest":
            result = run_ingest_job(db, worker_job)
        else:
            raise ValueError(f"Unknown job type: {worker_job.job_type!r}")

        worker_job.status = "done"
        worker_job.result_json = result
        worker_job.finished_at = datetime.now(tz=timezone.utc)
        db.flush()

        job_log.info("worker_job_done", **result)
        return True

    except Exception as exc:
        worker_job.status = "failed"
        worker_job.error = str(exc)
        worker_job.finished_at = datetime.now(tz=timezone.utc)
        db.flush()
        job_log.error("worker_job_failed", error=str(exc))
        return True  # we did process (even if it failed), don't sleep


def run_worker(poll_interval: int = POLL_INTERVAL):
    """Main worker loop. Runs until SIGINT/SIGTERM."""
    from src.db.session import get_db

    shutdown = False

    def _handle_signal(sig, frame):
        nonlocal shutdown
        log.info("worker_shutdown_requested", signal=sig)
        shutdown = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("worker_started", poll_interval=poll_interval)

    while not shutdown:
        try:
            with get_db() as db:
                processed = process_one(db)
            if not processed:
                # Nothing to do — sleep before next poll
                for _ in range(poll_interval * 10):
                    if shutdown:
                        break
                    time.sleep(0.1)
        except Exception as exc:
            log.error("worker_loop_error", error=str(exc))
            time.sleep(poll_interval)

    log.info("worker_stopped")
