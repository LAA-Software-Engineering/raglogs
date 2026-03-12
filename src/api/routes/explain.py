from datetime import datetime
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
    format: str = "json"


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

    try:
        with get_db() as db:
            result = explain_window(
                db=db,
                window_start=window_start,
                window_end=window_end,
                service=request.service,
                environment=request.env,
                no_llm=request.no_llm,
                max_clusters=request.max_clusters,
                baseline_window_str=request.baseline_window,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
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
    }
