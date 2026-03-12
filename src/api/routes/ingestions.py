import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

router = APIRouter()


class IngestRequest(BaseModel):
    paths: list[str]
    recursive: bool = False
    format: str = "auto"
    source_name: Optional[str] = None
    service: Optional[str] = None
    env: Optional[str] = None


class IngestResponse(BaseModel):
    job_id: str
    status: str
    files_found: int


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    file_count: int
    line_count: int
    parsed_count: int
    error_count: int


@router.post("", response_model=IngestResponse)
def create_ingestion(request: IngestRequest, background_tasks: BackgroundTasks):
    from src.core.ingestion.service import ingest_files
    from src.db.session import get_db
    from src.adapters.file.adapter import discover_files
    from pathlib import Path

    files = discover_files(request.paths, recursive=request.recursive)
    if not files:
        raise HTTPException(status_code=400, detail="No files found for given paths")

    # Run synchronously for simplicity in Phase 1
    try:
        with get_db() as db:
            job, stats = ingest_files(
                db=db,
                paths=request.paths,
                recursive=request.recursive,
                source_name=request.source_name,
                default_service=request.service,
                default_env=request.env,
                fmt=request.format,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return IngestResponse(
        job_id=str(job.id),
        status=job.status,
        files_found=len(files),
    )


@router.get("/{job_id}", response_model=JobStatusResponse)
def get_ingestion_status(job_id: str):
    from src.db.models import IngestionJob
    from src.db.session import get_db

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID")

    with get_db() as db:
        job = db.query(IngestionJob).filter(IngestionJob.id == job_uuid).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        return JobStatusResponse(
            job_id=str(job.id),
            status=job.status,
            file_count=job.file_count,
            line_count=job.line_count,
            parsed_count=job.parsed_count,
            error_count=job.error_count,
        )
