"""
Ingestion API routes.

POST /ingestions            — enqueue async ingest, return worker_job_id immediately
POST /ingestions/lines      — push NDJSON lines (sync persist)
POST /ingestions/{id}:pause|resume|stop — tail job lifecycle
GET  /ingestions            — list recent completed ingestion jobs (newest first)
GET  /ingestions/jobs/{id}  — poll worker job status (pending|running|done|failed)
GET  /ingestions/latest     — most recently completed ingestion job, if any
GET  /ingestions/{id}       — fetch IngestionJob detail by ingestion_job_id
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, model_validator

from src.core.ingestion.tail import TAIL_ADAPTERS

router = APIRouter()

LIST_INGESTIONS_DEFAULT_LIMIT = 25
LIST_INGESTIONS_MAX_LIMIT = 500


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
    mode: Literal["batch", "tail"] = "batch"
    callback_url: Optional[str] = None
    scope: Optional[str] = None

    @field_validator("callback_url")
    @classmethod
    def _validate_callback_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value.strip() == "":
            return None
        from src.core.ingestion.webhooks import validate_callback_url

        return validate_callback_url(value)

    @model_validator(mode="after")
    def _require_paths_for_file_adapter(self):
        if self.mode == "tail":
            if self.adapter not in TAIL_ADAPTERS:
                raise ValueError(
                    "mode='tail' requires adapter cloudwatch, datadog, or loki"
                )
            return self
        if self.adapter == "file" and not self.paths:
            raise ValueError("paths is required when adapter='file'")
        if self.adapter in ("k8s", "kubernetes"):
            params_paths = self.params.get("paths") or self.params.get("path")
            if not self.paths and not params_paths:
                raise ValueError("paths is required when adapter='k8s'")
        return self


class EnqueuedResponse(BaseModel):
    worker_job_id: Optional[str] = None  # set for batch enqueue; null for tail
    status: str  # pending (batch) | running (tail)
    ingestion_job_id: Optional[str] = None
    mode: str = "batch"


class WorkerJobStatus(BaseModel):
    worker_job_id: str
    status: str  # pending | running | done | failed
    ingestion_job_id: Optional[str]  # set once worker completes ingestion
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
    mode: str = "batch"
    last_polled_at: Optional[str] = None
    consecutive_errors: int = 0


class LatestIngestionResponse(BaseModel):
    ingestion_job_id: Optional[str]


class IngestionSummary(BaseModel):
    ingestion_job_id: str
    source_name: str
    parsed_count: int
    finished_at: Optional[str]


class IngestionListResponse(BaseModel):
    ingestions: list[IngestionSummary]


class PushLinesResponse(BaseModel):
    ingestion_job_id: str
    status: str
    line_count: int
    parsed_count: int
    error_count: int
    mode: str = "push"


class TailLifecycleResponse(BaseModel):
    ingestion_job_id: str
    mode: str
    status: str


# ── Helpers ──────────────────────────────────────────────────────────────────


def _queue_full_response() -> JSONResponse:
    from src.config import get_settings

    settings = get_settings()
    return JSONResponse(
        status_code=429,
        content={
            "error_code": "INGEST_QUEUE_FULL",
            "message": "Ingest worker queue is full; retry after the Retry-After delay.",
        },
        headers={"Retry-After": str(settings.ingest_retry_after_seconds)},
    )


def _reject_if_queue_full(db: Any) -> Optional[JSONResponse]:
    from src.config import get_settings
    from src.core.ingestion.backpressure import (
        ingest_queue_is_full,
        pending_worker_job_count,
    )

    settings = get_settings()
    pending = pending_worker_job_count(db)
    if ingest_queue_is_full(pending, settings.ingest_queue_max):
        return _queue_full_response()
    return None


def _validate_non_file_adapter(request: IngestRequest) -> dict[str, Any]:
    """Discover + window-validate a pull adapter. Returns possibly rewritten params."""
    from src.adapters.base import SourceSpec
    from src.adapters.registry import get_adapter
    from src.config import get_settings
    from src.core.errors import AdapterUnavailableError

    params = request.params
    if request.adapter in ("k8s", "kubernetes"):
        from src.adapters.k8s.adapter import build_k8s_params

        params = build_k8s_params(
            request.params, paths=request.paths, recursive=request.recursive
        )

    try:
        adapter = get_adapter(request.adapter, get_settings())
        refs = list(
            adapter.discover(SourceSpec(adapter=request.adapter, params=params))
        )
    except AdapterUnavailableError as e:
        raise HTTPException(
            status_code=400,
            detail={"error_code": e.error_code, "message": str(e)},
        )

    if request.adapter in ("k8s", "kubernetes") and not refs:
        raise HTTPException(
            status_code=400, detail="No log files found at the given paths"
        )

    if request.since or request.from_time or request.to_time:
        from src.utils.time import resolve_window

        try:
            resolve_window(
                since=request.since,
                from_time=request.from_time,
                to_time=request.to_time,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return params


def _ingest_payload(
    request: IngestRequest,
    params: dict[str, Any],
    *,
    callback_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
        "mode": request.mode,
    }
    if callback_meta:
        # scope / api_key_id belong on every ingest, not only webhook deliveries.
        payload.update(callback_meta)
    if request.callback_url:
        payload["callback_url"] = request.callback_url
    return payload


def _callback_meta_from_request(http_request: Request) -> dict[str, Any]:
    """Stash scope + api_key_id for HMAC at delivery time (not the bearer token)."""
    meta: dict[str, Any] = {"scope": "default"}
    resolved = getattr(http_request.state, "resolved_scope", None)
    if isinstance(resolved, str) and resolved.strip():
        meta["scope"] = resolved.strip()
    principal = getattr(http_request.state, "auth_principal", None)
    if principal is None:
        return meta
    if not isinstance(resolved, str) or not resolved.strip():
        scope = getattr(principal, "scope", None)
        if scope:
            meta["scope"] = scope
    key_id = getattr(principal, "key_id", None)
    if key_id:
        meta["api_key_id"] = key_id
    return meta


def _scope_from_request(http_request: Request) -> str:
    resolved = getattr(http_request.state, "resolved_scope", None)
    if isinstance(resolved, str) and resolved.strip():
        return resolved.strip()
    return str(_callback_meta_from_request(http_request).get("scope") or "default")


def _payload_scope(payload: Any, fallback: str = "default") -> str:
    if not isinstance(payload, dict):
        return fallback
    value = payload.get("scope")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _column_scope(job: Any, fallback: str = "default") -> str:
    value = getattr(job, "scope", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _scope_of_idempotency_row(db: Any, row: Any) -> str:
    """Scope of the job this Idempotency-Key is bound to (payload or column)."""
    worker_job_id = getattr(row, "worker_job_id", None)
    if worker_job_id is not None:
        from src.db.models import WorkerJob

        job = db.query(WorkerJob).filter(WorkerJob.id == worker_job_id).first()
        if job is not None:
            return _payload_scope(getattr(job, "payload_json", None))

    ingestion_job_id = getattr(row, "ingestion_job_id", None)
    if ingestion_job_id is not None:
        from src.db.models import IngestionJob

        job = db.query(IngestionJob).filter(IngestionJob.id == ingestion_job_id).first()
        if job is not None:
            column = _column_scope(job, fallback="")
            if column:
                return column
            meta = getattr(job, "metadata_json", None)
            if isinstance(meta, dict):
                return _payload_scope(meta)
    return "default"


def _idempotency_scope_conflict() -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error_code": "IDEMPOTENCY_SCOPE_CONFLICT",
            "message": (
                "Idempotency-Key is already bound to a different scope; "
                "use a distinct key or wait for the original mapping to expire."
            ),
        },
    )


def _enqueued_from_idempotency(row: Any) -> EnqueuedResponse:
    mode = getattr(row, "mode", None) or "batch"
    ingestion_job_id = getattr(row, "ingestion_job_id", None)
    worker_job_id = getattr(row, "worker_job_id", None)
    if mode == "tail":
        return EnqueuedResponse(
            worker_job_id=None,
            status="running",
            ingestion_job_id=str(ingestion_job_id) if ingestion_job_id else None,
            mode="tail",
        )
    return EnqueuedResponse(
        worker_job_id=str(worker_job_id) if worker_job_id else None,
        status="pending",
        ingestion_job_id=str(ingestion_job_id) if ingestion_job_id else None,
        mode="batch",
    )


def _replay_if_active(
    db: Any, key: Optional[str], scope: str
) -> Optional[EnqueuedResponse | JSONResponse]:
    if not key:
        return None
    from datetime import datetime, timezone

    from src.core.ingestion.idempotency import key_log_prefix, lookup_active_key

    row = lookup_active_key(db, key, datetime.now(tz=timezone.utc))
    if row is None:
        return None
    if _scope_of_idempotency_row(db, row) != scope:
        return _idempotency_scope_conflict()
    import structlog

    structlog.get_logger().info(
        "ingest_idempotency_replay",
        key_prefix=key_log_prefix(key),
        mode=getattr(row, "mode", None) or "batch",
    )
    return _enqueued_from_idempotency(row)


def _bind_idempotency_key(
    db: Any,
    key: Optional[str],
    *,
    worker_job_id: Optional[uuid.UUID],
    ingestion_job_id: Optional[uuid.UUID],
    mode: str,
    scope: str,
) -> Optional[EnqueuedResponse | JSONResponse]:
    """Persist the key mapping. Returns a replay response on unique-key race."""
    if not key:
        return None
    from datetime import datetime, timezone

    from src.config import get_settings
    from src.core.ingestion.idempotency import store_idempotency_key

    settings = get_settings()
    _row, is_replay = store_idempotency_key(
        db,
        key,
        worker_job_id=worker_job_id,
        ingestion_job_id=ingestion_job_id,
        mode=mode,
        now=datetime.now(tz=timezone.utc),
        ttl_seconds=settings.ingest_idempotency_ttl_seconds,
    )
    if is_replay:
        if _scope_of_idempotency_row(db, _row) != scope:
            return _idempotency_scope_conflict()
        return _enqueued_from_idempotency(_row)
    return None


def _create_tail_job(
    request: IngestRequest,
    params: dict[str, Any],
    db: Any,
    *,
    callback_meta: Optional[dict[str, Any]] = None,
) -> EnqueuedResponse:
    from src.core.ingestion.service import _get_or_create_source
    from src.db.models import IngestionJob

    source_name = request.source_name or f"tail:{request.adapter}"
    source = _get_or_create_source(db, name=source_name, type_=request.adapter)
    now = datetime.now(tz=timezone.utc)
    scope = "default"
    if callback_meta:
        raw = callback_meta.get("scope")
        if isinstance(raw, str) and raw.strip():
            scope = raw.strip()
    job = IngestionJob(
        id=uuid.uuid4(),
        source_id=source.id,
        status="running",
        started_at=now,
        metadata_json=_ingest_payload(request, params, callback_meta=callback_meta),
        source_adapter=request.adapter,
        source_ref=source_name,
        mode="tail",
        consecutive_errors=0,
        scope=scope,
    )
    db.add(job)
    db.flush()
    return EnqueuedResponse(
        worker_job_id=None,
        status="running",
        ingestion_job_id=str(job.id),
        mode="tail",
    )


def _parse_ingestion_job_id(ingestion_job_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(ingestion_job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ingestion_job_id")


def _apply_tail_action(
    ingestion_job_id: str,
    action: Literal["pause", "resume", "stop"],
    *,
    scope: str,
) -> TailLifecycleResponse:
    from src.core.ingestion.tail import TailLifecycleError, apply_tail_lifecycle
    from src.db.models import IngestionJob
    from src.db.session import get_db

    ij_uuid = _parse_ingestion_job_id(ingestion_job_id)
    with get_db() as db:
        job = (
            db.query(IngestionJob)
            .filter(IngestionJob.id == ij_uuid, IngestionJob.scope == scope)
            .first()
        )
        if not job:
            raise HTTPException(status_code=404, detail="Ingestion job not found")
        try:
            job.status = apply_tail_lifecycle(job.mode, job.status, action)
        except TailLifecycleError as e:
            raise HTTPException(status_code=409, detail=str(e))
        if action == "stop":
            job.finished_at = datetime.now(tz=timezone.utc)
        db.flush()
        return TailLifecycleResponse(
            ingestion_job_id=str(job.id),
            mode=job.mode,
            status=job.status,
        )


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("", response_model=EnqueuedResponse, status_code=202)
def create_ingestion(
    request: IngestRequest,
    http_request: Request,
    idempotency_key: Optional[str] = Header(
        default=None,
        alias="Idempotency-Key",
        description=(
            "If set, a repeat POST within INGEST_IDEMPOTENCY_TTL_SECONDS "
            "(default 86400) returns the original job instead of enqueueing a new one. "
            "Empty values are rejected with 400. Applies to batch enqueue and tail create."
        ),
    ),
):
    """
    Enqueue an ingest job. Returns immediately with worker_job_id.
    Poll GET /ingestions/jobs/{worker_job_id} for progress.
    When done, use ingestion_job_id from the result to scope /query/explain.

    ``mode=tail`` creates a long-lived job that the worker polls; the response
    includes ``ingestion_job_id`` and no worker_job_id.

    Optional ``callback_url`` (http/https) is stored on batch worker jobs. The
    worker POSTs an HMAC-signed completion payload on terminal state. Tail jobs
    do not fire callbacks (long-lived; poll or ``:stop`` instead).

    Optional ``Idempotency-Key`` (also accepted as ``idempotency-key``): a
    repeat within the TTL returns the original 202 job rather than a new one.
    """
    from src.api.auth.scope import bind_request_scope
    from src.core.ingestion.idempotency import (
        InvalidIdempotencyKey,
        parse_idempotency_key,
    )
    from src.db.models import WorkerJob
    from src.db.session import get_db

    resolved = bind_request_scope(http_request, request.scope)

    try:
        parsed_key = parse_idempotency_key(idempotency_key)
    except InvalidIdempotencyKey as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    params = request.params
    if request.adapter == "file":
        from src.adapters.file.adapter import discover_files

        files = discover_files(request.paths, recursive=request.recursive)
        if not files:
            raise HTTPException(
                status_code=400, detail="No log files found at the given paths"
            )
    else:
        params = _validate_non_file_adapter(request)

    callback_meta = _callback_meta_from_request(http_request)

    with get_db() as db:
        replayed = _replay_if_active(db, parsed_key, resolved)
        if replayed is not None:
            return replayed

        rejected = _reject_if_queue_full(db)
        if rejected is not None:
            return rejected

        if request.mode == "tail":
            response = _create_tail_job(
                request, params, db, callback_meta=callback_meta
            )
            race = _bind_idempotency_key(
                db,
                parsed_key,
                worker_job_id=None,
                ingestion_job_id=uuid.UUID(response.ingestion_job_id)
                if response.ingestion_job_id
                else None,
                mode="tail",
                scope=resolved,
            )
            if race is not None:
                from src.db.models import IngestionJob

                if response.ingestion_job_id:
                    orphan = (
                        db.query(IngestionJob)
                        .filter(IngestionJob.id == uuid.UUID(response.ingestion_job_id))
                        .first()
                    )
                    if orphan is not None:
                        db.delete(orphan)
                        db.flush()
                return race
            return response

        payload = _ingest_payload(request, params, callback_meta=callback_meta)
        job = WorkerJob(
            id=uuid.uuid4(),
            job_type="ingest",
            status="pending",
            payload_json=payload,
        )
        db.add(job)
        db.flush()
        worker_job_id = str(job.id)
        race = _bind_idempotency_key(
            db,
            parsed_key,
            worker_job_id=job.id,
            ingestion_job_id=None,
            mode="batch",
            scope=resolved,
        )
        if race is not None:
            db.delete(job)
            db.flush()
            return race

    return EnqueuedResponse(worker_job_id=worker_job_id, status="pending", mode="batch")


@router.get("", response_model=IngestionListResponse)
def list_ingestions(
    http_request: Request,
    scope: Optional[str] = None,
    limit: int = Query(
        LIST_INGESTIONS_DEFAULT_LIMIT,
        description=(
            "Maximum number of completed ingestions to return "
            f"(1–{LIST_INGESTIONS_MAX_LIMIT}). Default {LIST_INGESTIONS_DEFAULT_LIMIT}."
        ),
    ),
) -> IngestionListResponse:
    """List recent completed ingestion jobs, newest first. Used by the web UI's ingestion picker."""
    from sqlalchemy import desc, select

    from src.api.auth.scope import bind_request_scope
    from src.db.models import IngestionJob, Source
    from src.db.scope_filter import filter_ingestion_jobs_by_scope
    from src.db.session import get_db

    if limit < 1 or limit > LIST_INGESTIONS_MAX_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be between 1 and {LIST_INGESTIONS_MAX_LIMIT}",
        )

    resolved = bind_request_scope(http_request, scope)

    with get_db() as db:
        stmt = (
            select(IngestionJob, Source.name)
            .join(Source, Source.id == IngestionJob.source_id)
            .where(IngestionJob.status == "completed")
            .order_by(desc(IngestionJob.finished_at))
            .limit(limit)
        )
        stmt = filter_ingestion_jobs_by_scope(stmt, resolved)
        rows = db.execute(stmt).all()

        return IngestionListResponse(
            ingestions=[
                IngestionSummary(
                    ingestion_job_id=str(job.id),
                    source_name=source_name,
                    parsed_count=job.parsed_count,
                    finished_at=job.finished_at.isoformat()
                    if job.finished_at
                    else None,
                )
                for job, source_name in rows
            ]
        )


@router.get("/jobs/{worker_job_id}", response_model=WorkerJobStatus)
def get_worker_job_status(worker_job_id: str, http_request: Request):
    """
    Poll the status of an enqueued ingest job.
    When status == 'done', result.ingestion_job_id is ready for /query/explain.
    """
    from src.api.auth.scope import bind_request_scope
    from src.db.models import WorkerJob
    from src.db.session import get_db

    resolved = bind_request_scope(http_request, None)

    try:
        wj_uuid = uuid.UUID(worker_job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid worker_job_id")

    with get_db() as db:
        job = db.query(WorkerJob).filter(WorkerJob.id == wj_uuid).first()
        if not job:
            raise HTTPException(status_code=404, detail="Worker job not found")
        if _payload_scope(getattr(job, "payload_json", None)) != resolved:
            raise HTTPException(status_code=404, detail="Worker job not found")

        return WorkerJobStatus(
            worker_job_id=str(job.id),
            status=job.status,
            ingestion_job_id=str(job.ingestion_job_id)
            if job.ingestion_job_id
            else None,
            error=job.error,
            created_at=job.created_at.isoformat(),
            started_at=job.started_at.isoformat() if job.started_at else None,
            finished_at=job.finished_at.isoformat() if job.finished_at else None,
            result=job.result_json,
        )


@router.get("/latest", response_model=LatestIngestionResponse)
def get_latest_ingestion(http_request: Request, scope: Optional[str] = None):
    """
    Return the ID of the most recently completed ingestion job, or null if
    none exists yet — the same default the CLI and GET /ingestions apply
    (latest ingestion, not all ingestions merged). Not currently called by
    the web UI (its picker defaults to GET /ingestions[0] instead), but kept
    as a lighter-weight lookup for other integrations.

    Registered before /{ingestion_job_id} so "latest" isn't swallowed by
    that path param.
    """
    from src.api.auth.scope import bind_request_scope
    from src.core.explain.summarizer import get_latest_ingestion_job_id
    from src.db.session import get_db

    resolved = bind_request_scope(http_request, scope)

    with get_db() as db:
        job_id = get_latest_ingestion_job_id(db, scope=resolved)

    return LatestIngestionResponse(ingestion_job_id=str(job_id) if job_id else None)


@router.post(
    "/lines",
    response_model=PushLinesResponse,
    status_code=200,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/x-ndjson": {"schema": {"type": "string"}},
                "application/jsonl": {"schema": {"type": "string"}},
                "text/plain": {"schema": {"type": "string"}},
            },
        }
    },
)
async def push_ingestion_lines(request: Request) -> PushLinesResponse:
    """Push NDJSON of raw or pre-parsed log lines. Persisted synchronously."""
    from src.api.auth.scope import bind_request_scope
    from src.config import get_settings
    from src.core.ingestion.push import NdjsonParseError, parse_ndjson_payload
    from src.core.ingestion.service import ingest_push_lines
    from src.db.session import get_db

    bind_request_scope(request, None)

    settings = get_settings()
    body = (await request.body()).decode("utf-8", errors="replace")
    try:
        raw_lines = parse_ndjson_payload(body, settings.ingest_push_max_lines)
    except NdjsonParseError as e:
        raise HTTPException(status_code=400, detail=str(e))

    with get_db() as db:
        rejected = _reject_if_queue_full(db)
        if rejected is not None:
            return rejected  # type: ignore[return-value]
        job, stats = ingest_push_lines(
            db, raw_lines, scope=_scope_from_request(request)
        )

    return PushLinesResponse(
        ingestion_job_id=str(job.id),
        status=job.status,
        line_count=stats.lines_read,
        parsed_count=stats.parsed_count,
        error_count=stats.error_count,
        mode="push",
    )


@router.get("/lines", include_in_schema=False)
def push_lines_get_not_allowed() -> None:
    """Static /lines must not fall through to /{ingestion_job_id} UUID parsing."""
    raise HTTPException(status_code=405, detail="Method Not Allowed")


@router.post("/{ingestion_job_id}:pause", response_model=TailLifecycleResponse)
def pause_tail_job(ingestion_job_id: str, http_request: Request) -> TailLifecycleResponse:
    from src.api.auth.scope import bind_request_scope

    scope = bind_request_scope(http_request, None)
    return _apply_tail_action(ingestion_job_id, "pause", scope=scope)


@router.post("/{ingestion_job_id}:resume", response_model=TailLifecycleResponse)
def resume_tail_job(ingestion_job_id: str, http_request: Request) -> TailLifecycleResponse:
    from src.api.auth.scope import bind_request_scope

    scope = bind_request_scope(http_request, None)
    return _apply_tail_action(ingestion_job_id, "resume", scope=scope)


@router.post("/{ingestion_job_id}:stop", response_model=TailLifecycleResponse)
def stop_tail_job(ingestion_job_id: str, http_request: Request) -> TailLifecycleResponse:
    from src.api.auth.scope import bind_request_scope

    scope = bind_request_scope(http_request, None)
    return _apply_tail_action(ingestion_job_id, "stop", scope=scope)


@router.get("/{ingestion_job_id}", response_model=IngestionJobDetail)
def get_ingestion_detail(ingestion_job_id: str, http_request: Request):
    """Fetch an IngestionJob record by its ID."""
    from src.api.auth.scope import bind_request_scope
    from src.db.models import IngestionJob
    from src.db.session import get_db

    scope = bind_request_scope(http_request, None)
    ij_uuid = _parse_ingestion_job_id(ingestion_job_id)

    with get_db() as db:
        job = (
            db.query(IngestionJob)
            .filter(IngestionJob.id == ij_uuid, IngestionJob.scope == scope)
            .first()
        )
        if not job:
            raise HTTPException(status_code=404, detail="Ingestion job not found")

        polled = getattr(job, "last_polled_at", None)
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
            mode=getattr(job, "mode", None) or "batch",
            last_polled_at=polled.isoformat() if isinstance(polled, datetime) else None,
            consecutive_errors=int(getattr(job, "consecutive_errors", 0) or 0),
        )
