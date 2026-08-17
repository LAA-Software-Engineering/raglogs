"""Health endpoint — includes worker queue depth and per-adapter availability."""
import time
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

_ADAPTER_HEALTH_TTL = 30.0  # seconds — avoid hitting the AWS credential chain (possibly
                            # IMDS) on every single /health call from a liveness probe
_adapter_health_cache: dict[str, tuple[float, str]] = {}


class LlmBreakerHealth(BaseModel):
    state: str  # closed | open | half_open
    consecutive_failures: int
    cooldown_remaining_seconds: float


class HealthResponse(BaseModel):
    status: str           # ok | degraded
    db: str                # connected | disconnected
    worker_queue_depth: Optional[int]   # pending worker jobs; None if DB unreachable
    adapters: dict[str, str]            # adapter name -> "ok" | "unavailable: <reason>"
    tail_jobs: Optional[dict[str, int]] = None  # {running, paused}; None if DB unreachable
    llm_breaker: Optional[LlmBreakerHealth] = None


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


def _llm_breaker_health() -> LlmBreakerHealth:
    from src.core.llm.resilience import breaker_health

    snap = breaker_health()
    return LlmBreakerHealth(
        state=str(snap["state"]),
        consecutive_failures=int(snap["consecutive_failures"]),
        cooldown_remaining_seconds=float(snap["cooldown_remaining_seconds"]),
    )


@router.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    from src.db.session import check_connection, get_db

    db_ok = check_connection()
    adapters = _adapter_health()
    llm_breaker = _llm_breaker_health()
    breaker_open = llm_breaker.state == "open"

    if not db_ok:
        return HealthResponse(
            status="degraded",
            db="disconnected",
            worker_queue_depth=None,
            adapters=adapters,
            tail_jobs=None,
            llm_breaker=llm_breaker,
        )

    try:
        from sqlalchemy import func, select

        from src.core.ingestion.tail import tail_job_counts
        from src.db.models import WorkerJob
        with get_db() as db:
            depth = db.execute(
                select(func.count()).select_from(WorkerJob).where(WorkerJob.status == "pending")
            ).scalar_one()
            tail_jobs = tail_job_counts(db)
    except Exception:
        depth = None
        tail_jobs = None

    return HealthResponse(
        status="degraded" if breaker_open else "ok",
        db="connected",
        worker_queue_depth=depth,
        adapters=adapters,
        tail_jobs=tail_jobs,
        llm_breaker=llm_breaker,
    )
