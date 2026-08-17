"""
Explain API route with caching.

POST /query/explain
  - optional ingestion_job_id scopes analysis to a specific ingest
  - caches results in the explanations table keyed on (window, service, env, ingestion_job_id)
  - returns cached result on repeated calls for the same window+filters
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.api.schemas.v1 import (
    ExplainResponse,
    explain_from_cached,
    explain_from_result,
)

if TYPE_CHECKING:
    from src.core.explain.summarizer import ExplainResult

router = APIRouter()


class ExplainRequest(BaseModel):
    since: Optional[str] = None
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    service: Optional[str] = None
    env: Optional[str] = None
    no_llm: bool = False
    max_clusters: int = 10
    baseline_window: Optional[str] = None
    ingestion_job_id: Optional[str] = None   # scope to a specific ingest
    force_refresh: bool = False               # bypass cache
    format: Literal["json", "markdown"] = "json"
    scope: Optional[str] = None


def _cache_key(window_start: datetime, window_end: datetime,
               service: Optional[str], env: Optional[str],
               ingestion_job_id: Optional[str],
               scope: str = "default") -> str:
    """SHA-256 of the canonical filter string — used to detect cache hits."""
    canonical = json.dumps({
        "ws": window_start.isoformat(),
        "we": window_end.isoformat(),
        "svc": service,
        "env": env,
        "ijid": ingestion_job_id,
        "scope": scope,
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _load_from_cache(db, cache_hash: str) -> Optional[dict]:
    """Return cached explanation dict if one exists, else None."""
    from src.db.models import Explanation
    from sqlalchemy import select

    row = db.execute(
        select(Explanation)
        .where(Explanation.prompt_hash == cache_hash)
        .order_by(Explanation.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if row and row.result_json:
        return row.result_json
    return None


def _confidence_label(payload: dict) -> str:
    conf = payload.get("confidence")
    if isinstance(conf, dict):
        return str(conf.get("label") or "low")
    return str(conf or "low")


def _evidence_strings(payload: dict) -> list[str]:
    items = payload.get("evidence") or []
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            out.append(str(item.get("detail") or item))
        else:
            out.append(str(item))
    return out


def _primary_for_markdown(payload: dict) -> Optional[dict]:
    pc = payload.get("primary_cluster")
    if not isinstance(pc, dict):
        return pc
    if pc.get("message") or not pc.get("template"):
        return pc
    return {**pc, "message": pc.get("template")}


def _trigger_candidates_for_markdown(payload: dict) -> list[dict]:
    candidates = payload.get("trigger_candidates")
    if isinstance(candidates, list) and candidates:
        return list(candidates)
    trigger = payload.get("trigger") or {}
    if isinstance(trigger, dict) and trigger.get("detected"):
        return [{
            "message": trigger.get("type") or trigger.get("detail") or "",
            "timestamp": trigger.get("at"),
            "service": trigger.get("service"),
        }]
    return []


def _explain_result_from_payload(
    payload: dict,
    window_start: datetime,
    window_end: datetime,
) -> ExplainResult:
    """Rebuild an ExplainResult from a cached/API payload for markdown rendering."""
    from src.core.explain.summarizer import ExplainResult as ExplainResultCls

    prose = payload.get("rendered_text") or payload.get("summary") or ""
    return ExplainResultCls(
        window_start=window_start,
        window_end=window_end,
        summary_text=prose,
        confidence=_confidence_label(payload),
        evidence_items=_evidence_strings(payload),
        services_affected=list(payload.get("services_affected") or []),
        primary_cluster=_primary_for_markdown(payload),
        secondary_clusters=list(payload.get("secondary_clusters") or []),
        trigger_candidates=_trigger_candidates_for_markdown(payload),
        total_logs=int(payload.get("total_logs") or 0),
        mode=payload.get("mode") or "rules",
    )


def _maybe_add_markdown(
    body: ExplainResponse,
    result: ExplainResult,
    request: ExplainRequest,
) -> ExplainResponse:
    if request.format != "markdown":
        return body
    from src.core.explain.markdown_report import render_incident_report

    return body.model_copy(
        update={"markdown": render_incident_report(result, environment=request.env)}
    )


def _save_to_cache(db, cache_hash: str, window_start: datetime, window_end: datetime,
                   service: Optional[str], env: Optional[str], result: dict, confidence: str, mode: str):
    """Persist an explanation result to the cache."""
    from src.db.models import Explanation

    row = Explanation(
        id=uuid.uuid4(),
        window_start=window_start,
        window_end=window_end,
        service_filter=service,
        environment_filter=env,
        mode=mode,
        prompt_hash=cache_hash,
        result_json=result,
        confidence=confidence,
    )
    db.add(row)
    db.flush()


@router.post(
    "/explain",
    response_model=ExplainResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
)
def explain_endpoint(request: ExplainRequest, http_request: Request) -> ExplainResponse:
    from src.api.auth.scope import bind_request_scope
    from src.core.explain.summarizer import explain_window
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

    cache_hash = _cache_key(
        window_start, window_end, request.service, request.env, request.ingestion_job_id, scope
    )

    try:
        with get_db() as db:
            # Cache check — skip if force_refresh or no_llm (rules-only is fast)
            if not request.force_refresh and not request.no_llm:
                cached = _load_from_cache(db, cache_hash)
                if cached:
                    body = explain_from_cached(
                        cached,
                        window_start=window_start,
                        window_end=window_end,
                        no_llm=request.no_llm,
                        scope=scope,
                    )
                    cached_result = _explain_result_from_payload(
                        body.model_dump(by_alias=True), window_start, window_end
                    )
                    return _maybe_add_markdown(body, cached_result, request)

            result = explain_window(
                db=db,
                window_start=window_start,
                window_end=window_end,
                service=request.service,
                environment=request.env,
                no_llm=request.no_llm,
                max_clusters=request.max_clusters,
                baseline_window_str=request.baseline_window,
                ingestion_job_id=ingestion_job_id,
                scope=scope,
            )

            body = explain_from_result(
                result,
                no_llm=request.no_llm,
                cached=False,
                scope=scope,
            )

            # Persist to cache (only rules mode — LLM results are expensive and should
            # be explicitly refreshed; rules results are deterministic for the same window)
            if result.mode == "rules":
                cache_payload = body.model_dump(by_alias=True, exclude_unset=True)
                cache_payload.pop("cached", None)
                cache_payload.pop("markdown", None)
                _save_to_cache(
                    db, cache_hash, window_start, window_end,
                    request.service, request.env, cache_payload, result.confidence, result.mode,
                )

            return _maybe_add_markdown(body, result, request)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
