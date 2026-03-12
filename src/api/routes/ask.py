from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class AskRequest(BaseModel):
    question: str
    since: Optional[str] = None
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    service: Optional[str] = None


@router.post("/ask")
def ask_endpoint(request: AskRequest):
    from src.core.retrieval.question_router import answer_question
    from src.db.session import get_db
    from src.utils.time import resolve_window

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

    try:
        with get_db() as db:
            result = answer_question(
                db=db,
                question=request.question,
                window_start=window_start,
                window_end=window_end,
                service=request.service,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "question": result.question,
        "answer": result.answer_text,
        "evidence": result.evidence_items,
        "clusters": result.clusters_used,
        "total_matches": result.total_matches,
    }
