from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class ClustersRequest(BaseModel):
    since: Optional[str] = None
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    service: Optional[str] = None
    env: Optional[str] = None
    top: int = 15


@router.post("/clusters")
def clusters_endpoint(request: ClustersRequest):
    from src.core.clustering.clusterer import run_clustering
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
            _, clusters = run_clustering(
                db=db,
                window_start=window_start,
                window_end=window_end,
                service=request.service,
                environment=request.env,
                max_clusters=request.top,
                save_to_db=False,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "clusters": [
            {
                "fingerprint": c.fingerprint,
                "message": c.representative_message,
                "count": c.count,
                "services": list(c.services.keys()),
                "levels": c.levels,
                "first_seen": c.first_seen.isoformat() if c.first_seen else None,
                "last_seen": c.last_seen.isoformat() if c.last_seen else None,
                "baseline_count": c.baseline_count,
                "change_ratio": round(c.change_ratio, 2),
                "importance_score": round(c.importance_score, 2),
                "is_trigger": c.is_trigger,
            }
            for c in clusters
        ],
    }
