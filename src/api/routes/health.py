"""Health endpoint — includes worker queue depth and per-adapter availability."""
import time
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

_ADAPTER_HEALTH_TTL = 30.0  # seconds — avoid hitting the AWS credential chain (possibly
                            # IMDS) on every single /health call from a liveness probe
_adapter_health_cache: dict[str, tuple[float, str]] = {}


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
    now = time.monotonic()
    statuses: dict[str, str] = {}

    for name in ADAPTER_NAMES:
        cached = _adapter_health_cache.get(name)
        if cached is not None and now - cached[0] < _ADAPTER_HEALTH_TTL:
            statuses[name] = cached[1]
            continue

        try:
            adapter = get_adapter(name, settings)
            check = getattr(adapter, "check_available", None)
            if check is not None:
                check()
            status = "ok"
        except AdapterUnavailableError as e:
            status = f"unavailable: {e}"
        except Exception as e:
            status = f"unavailable: {e}"

        _adapter_health_cache[name] = (now, status)
        statuses[name] = status

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
