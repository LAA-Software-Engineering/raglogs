"""
Ingestion API routes.

POST /ingestions            — enqueue async ingest, return worker_job_id immediately
GET  /ingestions/jobs/{id}  — poll worker job status (pending|running|done|failed)
GET  /ingestions/{id}       — fetch completed IngestionJob detail by ingestion_job_id
"""
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    paths: list[str]
    recursive: bool = False
    format: str = "auto"
    source_name: Optional[str] = None
    service: Optional[str] = None
    env: Optional[str] = None


class EnqueuedResponse(BaseModel):
    worker_job_id: str
    status: str  # always "pending" at enqueue time


class WorkerJobStatus(BaseModel):
    worker_job_id: str
    status: str                        # pending | running | done | failed
    ingestion_job_id: Optional[str]    # set once worker completes ingestion
    error: Optional[str]
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    result: Optional[dict]


class IngestionJobDetail(BaseModel):
    ingestion_job_id: str
    status: str
    file_count: int
    line_count: int
    parsed_count: int
    error_count: int
    error_message: Optional[str]
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("", response_model=EnqueuedResponse, status_code=202)
def create_ingestion(request: IngestRequest):
    """
    Enqueue an ingest job. Returns immediately with worker_job_id.
    Poll GET /ingestions/jobs/{worker_job_id} for progress.
    When done, use ingestion_job_id from the result to scope /query/explain.
    """
    from src.adapters.file.adapter import discover_files
    from src.db.models import WorkerJob
    from src.db.session import get_db

    # Validate paths before enqueuing — fail fast
    files = discover_files(request.paths, recursive=request.recursive)
    if not files:
        raise HTTPException(status_code=400, detail="No log files found at the given paths")

    payload = {
        "paths": request.paths,
        "recursive": request.recursive,
        "format": request.format,
        "source_name": request.source_name,
        "service": request.service,
        "env": request.env,
    }

    with get_db() as db:
        job = WorkerJob(
            id=uuid.uuid4(),
            job_type="ingest",
            status="pending",
            payload_json=payload,
        )
        db.add(job)
        db.flush()
        worker_job_id = str(job.id)

    return EnqueuedResponse(worker_job_id=worker_job_id, status="pending")


@router.get("/jobs/{worker_job_id}", response_model=WorkerJobStatus)
def get_worker_job_status(worker_job_id: str):
    """
    Poll the status of an enqueued ingest job.
    When status == 'done', result.ingestion_job_id is ready for /query/explain.
    """
    from src.db.models import WorkerJob
    from src.db.session import get_db

    try:
        wj_uuid = uuid.UUID(worker_job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid worker_job_id")

    with get_db() as db:
        job = db.query(WorkerJob).filter(WorkerJob.id == wj_uuid).first()
        if not job:
            raise HTTPException(status_code=404, detail="Worker job not found")

        return WorkerJobStatus(
            worker_job_id=str(job.id),
            status=job.status,
            ingestion_job_id=str(job.ingestion_job_id) if job.ingestion_job_id else None,
            error=job.error,
            created_at=job.created_at.isoformat(),
            started_at=job.started_at.isoformat() if job.started_at else None,
            finished_at=job.finished_at.isoformat() if job.finished_at else None,
            result=job.result_json,
        )


@router.get("/{ingestion_job_id}", response_model=IngestionJobDetail)
def get_ingestion_detail(ingestion_job_id: str):
    """Fetch an IngestionJob record by its ID."""
    from src.db.models import IngestionJob
    from src.db.session import get_db

    try:
        ij_uuid = uuid.UUID(ingestion_job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ingestion_job_id")

    with get_db() as db:
        job = db.query(IngestionJob).filter(IngestionJob.id == ij_uuid).first()
        if not job:
            raise HTTPException(status_code=404, detail="Ingestion job not found")

        return IngestionJobDetail(
            ingestion_job_id=str(job.id),
            status=job.status,
            file_count=job.file_count,
            line_count=job.line_count,
            parsed_count=job.parsed_count,
            error_count=job.error_count,
            error_message=job.error_message,
            created_at=job.created_at.isoformat(),
            started_at=job.started_at.isoformat() if job.started_at else None,
            finished_at=job.finished_at.isoformat() if job.finished_at else None,
        )
