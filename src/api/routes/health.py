"""Health endpoint — includes worker queue depth."""
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

router = APIRouter()


class HealthResponse(BaseModel):
    status: str           # ok | degraded
    db: str               # connected | disconnected
    worker_queue_depth: Optional[int]   # pending worker jobs; None if DB unreachable


@router.get("/health", response_model=HealthResponse)
def health_check():
    from src.db.session import check_connection, get_db

    db_ok = check_connection()
    if not db_ok:
        return HealthResponse(status="degraded", db="disconnected", worker_queue_depth=None)

    try:
        from src.db.models import WorkerJob
        from sqlalchemy import func, select
        with get_db() as db:
            depth = db.execute(
                select(func.count()).select_from(WorkerJob).where(WorkerJob.status == "pending")
            ).scalar_one()
    except Exception:
        depth = None

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        db="connected",
        worker_queue_depth=depth,
    )
