"""POST /query/timeline — same pipeline as `raglogs timeline`."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.api.schemas.v1 import (
    SCHEMA_VERSION,
    TimelineEventModel,
    TimelineResponse,
    llm_rules_only,
    window_from_bounds,
)
from src.core.timeline.plain_text import format_timeline_plain

router = APIRouter()


class TimelineRequest(BaseModel):
    since: Optional[str] = None
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    service: Optional[str] = None
    env: Optional[str] = None
    all_ingestions: bool = False
    ingestion_job_id: Optional[str] = None
    format: Literal["json", "text"] = "json"
    scope: Optional[str] = None


def _events_to_models(events) -> list[TimelineEventModel]:
    return [
        TimelineEventModel(
            timestamp=e.timestamp.isoformat(),
            category=e.category,
            label=e.label,
            description=e.description,
            count=e.count,
            services=e.services,
            duration_minutes=e.duration_minutes,
        )
        for e in events
    ]


@router.post(
    "/timeline",
    response_model=TimelineResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
)
def timeline_endpoint(request: TimelineRequest, http_request: Request) -> TimelineResponse:
    from src.api.auth.scope import bind_request_scope
    from src.core.clustering.clusterer import run_clustering
    from src.core.explain.evidence import assemble_evidence
    from src.core.explain.summarizer import get_latest_ingestion_job_id
    from src.core.timeline.builder import build_timeline
    from src.db.session import get_db
    from src.utils.time import resolve_window

    scope = bind_request_scope(http_request, request.scope)

    try:
        window_start, window_end = resolve_window(
            since=request.since,
            from_time=request.from_time,
            to_time=request.to_time,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ingestion_job_id: Optional[uuid.UUID] = None
    if request.ingestion_job_id:
        try:
            ingestion_job_id = uuid.UUID(request.ingestion_job_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid ingestion_job_id")

    try:
        with get_db() as db:
            job_id = None
            if ingestion_job_id is not None:
                job_id = ingestion_job_id
            elif not request.all_ingestions:
                job_id = get_latest_ingestion_job_id(db, scope=scope)

            _, clusters = run_clustering(
                db=db,
                window_start=window_start,
                window_end=window_end,
                service=request.service,
                environment=request.env,
                save_to_db=False,
                ingestion_job_id=job_id,
                scope=scope,
            )

            packet = assemble_evidence(
                db=db,
                window_start=window_start,
                window_end=window_end,
                clusters=clusters,
                service_filter=request.service,
                environment_filter=request.env,
                ingestion_job_id=job_id,
                scope=scope,
            )

            events = build_timeline(packet)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    body = TimelineResponse(
        schema_version=SCHEMA_VERSION,
        scope=scope,
        window=window_from_bounds(window_start, window_end),
        events=_events_to_models(events),
        llm=llm_rules_only(),
        ingestion_job_id=request.ingestion_job_id,
        all_ingestions=request.all_ingestions,
    )
    if request.format == "text":
        rendered = format_timeline_plain(events, window_start, window_end)
        body = body.model_copy(update={"rendered_text": rendered, "text": rendered})
    return body
