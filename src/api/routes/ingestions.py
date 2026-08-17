"""
Ingestion API routes.

POST /ingestions            — enqueue async ingest, return worker_job_id immediately
GET  /ingestions            — list recent completed ingestion jobs (newest first)
GET  /ingestions/jobs/{id}  — poll worker job status (pending|running|done|failed)
GET  /ingestions/latest     — most recently completed ingestion job, if any
GET  /ingestions/{id}       — fetch completed IngestionJob detail by ingestion_job_id
"""
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, model_validator

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    paths: list[str] = []
    recursive: bool = False
    format: str = "auto"
    source_name: Optional[str] = None
    service: Optional[str] = None
    env: Optional[str] = None
    adapter: str = "file"
    params: dict[str, Any] = {}
    # Window for non-file adapters — same since/from_time/to_time contract as
    # POST /query/explain, resolved via src.utils.time.resolve_window (handles a
    # single bound and attaches UTC to naive datetimes). Omit all three to default
    # to the last 1h.
    since: Optional[str] = None
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    resume_ingestion_job_id: Optional[str] = None
    with_embeddings: bool = False

    @model_validator(mode="after")
    def _require_paths_for_file_adapter(self):
        if self.adapter == "file" and not self.paths:
            raise ValueError("paths is required when adapter='file'")
        if self.adapter in ("k8s", "kubernetes"):
            params_paths = self.params.get("paths") or self.params.get("path")
            if not self.paths and not params_paths:
                raise ValueError("paths is required when adapter='k8s'")
        return self


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
    source_adapter: str
    source_ref: Optional[str]
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]


class LatestIngestionResponse(BaseModel):
    ingestion_job_id: Optional[str]


class IngestionSummary(BaseModel):
    ingestion_job_id: str
    source_name: str
    parsed_count: int
    finished_at: Optional[str]


class IngestionListResponse(BaseModel):
    ingestions: list[IngestionSummary]


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("", response_model=EnqueuedResponse, status_code=202)
def create_ingestion(request: IngestRequest):
    """
    Enqueue an ingest job. Returns immediately with worker_job_id.
    Poll GET /ingestions/jobs/{worker_job_id} for progress.
    When done, use ingestion_job_id from the result to scope /query/explain.
    """
    from src.db.models import WorkerJob
    from src.db.session import get_db

    params = request.params
    if request.adapter == "file":
        from src.adapters.file.adapter import discover_files

        # Validate paths before enqueuing — fail fast
        files = discover_files(request.paths, recursive=request.recursive)
        if not files:
            raise HTTPException(status_code=400, detail="No log files found at the given paths")
    else:
        from src.adapters.base import SourceSpec
        from src.adapters.registry import get_adapter
        from src.config import get_settings
        from src.core.errors import AdapterUnavailableError

        if request.adapter in ("k8s", "kubernetes"):
            from src.adapters.k8s.adapter import build_k8s_params

            params = build_k8s_params(
                request.params, paths=request.paths, recursive=request.recursive
            )

        try:
            adapter = get_adapter(request.adapter, get_settings())
            # Fail fast on missing/invalid params (e.g. no log_group / query) —
            # discover() for pull adapters is local-only validation, no network call.
            refs = list(adapter.discover(SourceSpec(adapter=request.adapter, params=params)))
        except AdapterUnavailableError as e:
            raise HTTPException(
                status_code=400,
                detail={"error_code": e.error_code, "message": str(e)},
            )

        if request.adapter in ("k8s", "kubernetes") and not refs:
            raise HTTPException(status_code=400, detail="No log files found at the given paths")

        if request.since or request.from_time or request.to_time:
            # Same validation POST /query/explain does at the route level — otherwise
            # an unparseable `since` (e.g. "not-a-duration") 202s here and only fails
            # once the worker picks it up.
            from src.utils.time import resolve_window

            try:
                resolve_window(since=request.since, from_time=request.from_time, to_time=request.to_time)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

    payload = {
        "paths": request.paths,
        "recursive": request.recursive,
        "format": request.format,
        "source_name": request.source_name,
        "service": request.service,
        "env": request.env,
        "adapter": request.adapter,
        "params": params,
        "since": request.since,
        "from_time": request.from_time.isoformat() if request.from_time else None,
        "to_time": request.to_time.isoformat() if request.to_time else None,
        "resume_ingestion_job_id": request.resume_ingestion_job_id,
        "with_embeddings": request.with_embeddings,
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


@router.get("", response_model=IngestionListResponse)
def list_ingestions():
    """List recent completed ingestion jobs, newest first. Used by the web UI's ingestion picker."""
    from sqlalchemy import desc, select

    from src.db.models import IngestionJob, Source
    from src.db.session import get_db

    with get_db() as db:
        rows = db.execute(
            select(IngestionJob, Source.name)
            .join(Source, Source.id == IngestionJob.source_id)
            .where(IngestionJob.status == "completed")
            .order_by(desc(IngestionJob.finished_at))
            .limit(25)
        ).all()

        return IngestionListResponse(
            ingestions=[
                IngestionSummary(
                    ingestion_job_id=str(job.id),
                    source_name=source_name,
                    parsed_count=job.parsed_count,
                    finished_at=job.finished_at.isoformat() if job.finished_at else None,
                )
                for job, source_name in rows
            ]
        )


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


@router.get("/latest", response_model=LatestIngestionResponse)
def get_latest_ingestion():
    """
    Return the ID of the most recently completed ingestion job, or null if
    none exists yet — the same default the CLI and GET /ingestions apply
    (latest ingestion, not all ingestions merged). Not currently called by
    the web UI (its picker defaults to GET /ingestions[0] instead), but kept
    as a lighter-weight lookup for other integrations.

    Registered before /{ingestion_job_id} so "latest" isn't swallowed by
    that path param.
    """
    from src.core.explain.summarizer import get_latest_ingestion_job_id
    from src.db.session import get_db

    with get_db() as db:
        job_id = get_latest_ingestion_job_id(db)

    return LatestIngestionResponse(ingestion_job_id=str(job_id) if job_id else None)


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
            source_adapter=job.source_adapter,
            source_ref=job.source_ref,
            created_at=job.created_at.isoformat(),
            started_at=job.started_at.isoformat() if job.started_at else None,
            finished_at=job.finished_at.isoformat() if job.finished_at else None,
        )
