import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.api.overrides import QueryOverrideFields
from src.api.schemas.v1 import (
    SCHEMA_VERSION,
    AskResponse,
    llm_from_overrides,
)

router = APIRouter()


class AskRequest(QueryOverrideFields):
    question: str
    since: Optional[str] = None
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    service: Optional[str] = None
    ingestion_job_id: Optional[str] = None
    scope: Optional[str] = None
    no_llm: Optional[bool] = None


@router.post(
    "/ask",
    response_model=AskResponse,
    response_model_exclude_unset=True,
)
def ask_endpoint(request: AskRequest, http_request: Request) -> AskResponse:
    from src.api.auth.scope import bind_request_scope
    from src.api.overrides import resolve_overrides_from_http
    from src.core.retrieval.question_router import answer_question
    from src.db.session import get_db
    from src.utils.time import resolve_window

    scope = bind_request_scope(http_request, request.scope)
    overrides = resolve_overrides_from_http(http_request, request)

    window_start, window_end = None, None
    if request.since or request.from_time:
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
            result = answer_question(
                db=db,
                question=request.question,
                window_start=window_start,
                window_end=window_end,
                service=request.service,
                ingestion_job_id=ingestion_job_id,
                scope=scope,
                no_llm=overrides.no_llm,
                max_clusters=overrides.max_clusters,
                max_evidence_items=overrides.max_evidence_items,
                llm_provider=overrides.llm_provider,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return AskResponse(
        schema_version=SCHEMA_VERSION,
        question=result.question,
        answer=result.answer_text,
        evidence=result.evidence_items,
        clusters=result.clusters_used,
        total_matches=result.total_matches,
        retrieval_mode=result.retrieval_mode,
        llm=llm_from_overrides(
            mode=result.mode,
            llm_provider=overrides.llm_provider,
            llm_enabled=overrides.llm_enabled,
        ),
        rendered_text=result.answer_text,
        mode=result.mode,
    )
