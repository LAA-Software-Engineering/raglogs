"""
Explain API route with caching.

POST /query/explain
  - optional ingestion_job_id scopes analysis to a specific ingest
  - caches results in the explanations table keyed on (window, service, env, ingestion_job_id)
  - returns cached result on repeated calls for the same window+filters
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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


def _cache_key(window_start: datetime, window_end: datetime,
               service: Optional[str], env: Optional[str],
               ingestion_job_id: Optional[str]) -> str:
    """SHA-256 of the canonical filter string — used to detect cache hits."""
    canonical = json.dumps({
        "ws": window_start.isoformat(),
        "we": window_end.isoformat(),
        "svc": service,
        "env": env,
        "ijid": ingestion_job_id,
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


@router.post("/explain")
def explain_endpoint(request: ExplainRequest):
    from src.core.explain.summarizer import explain_window
    from src.db.session import get_db
    from src.utils.time import resolve_window

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

    cache_hash = _cache_key(window_start, window_end, request.service, request.env, request.ingestion_job_id)

    try:
        with get_db() as db:
            # Cache check — skip if force_refresh or no_llm (rules-only is fast)
            if not request.force_refresh and not request.no_llm:
                cached = _load_from_cache(db, cache_hash)
                if cached:
                    return {**cached, "cached": True}

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
            )

            payload = {
                "window": {
                    "start": result.window_start.isoformat(),
                    "end": result.window_end.isoformat(),
                },
                "summary": result.summary_text,
                "confidence": result.confidence,
                "mode": result.mode,
                "total_logs": result.total_logs,
                "services_affected": result.services_affected,
                "primary_cluster": result.primary_cluster,
                "secondary_clusters": result.secondary_clusters,
                "trigger_candidates": result.trigger_candidates,
                "evidence": result.evidence_items,
                "cached": False,
            }

            # Persist to cache (only rules mode — LLM results are expensive and should
            # be explicitly refreshed; rules results are deterministic for the same window)
            if result.mode == "rules":
                _save_to_cache(
                    db, cache_hash, window_start, window_end,
                    request.service, request.env, payload, result.confidence, result.mode,
                )

            return payload

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
