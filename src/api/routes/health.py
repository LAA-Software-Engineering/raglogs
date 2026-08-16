"""Health endpoint — includes worker queue depth and per-adapter availability."""
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

router = APIRouter()


class HealthResponse(BaseModel):
    status: str           # ok | degraded
    db: str                # connected | disconnected
    worker_queue_depth: Optional[int]   # pending worker jobs; None if DB unreachable
    adapters: dict[str, str]            # adapter name -> "ok" | "unavailable: <reason>"


def _adapter_health() -> dict[str, str]:
    from src.adapters.registry import ADAPTER_NAMES, get_adapter
    from src.config import get_settings
    from src.core.errors import AdapterUnavailableError

    settings = get_settings()
    statuses: dict[str, str] = {}
    for name in ADAPTER_NAMES:
        try:
            adapter = get_adapter(name, settings)
            check = getattr(adapter, "check_available", None)
            if check is not None:
                check()
            statuses[name] = "ok"
        except AdapterUnavailableError as e:
            statuses[name] = f"unavailable: {e}"
        except Exception as e:
            statuses[name] = f"unavailable: {e}"
    return statuses


@router.get("/health", response_model=HealthResponse)
def health_check():
    from src.db.session import check_connection, get_db

    db_ok = check_connection()
    adapters = _adapter_health()

    if not db_ok:
        return HealthResponse(status="degraded", db="disconnected", worker_queue_depth=None, adapters=adapters)

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
        adapters=adapters,
    )
