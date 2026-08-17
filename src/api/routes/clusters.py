"""Clusters API route with optional ingestion_job_id scoping."""
import uuid
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
    ingestion_job_id: Optional[str] = None


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

    ingestion_job_id: Optional[uuid.UUID] = None
    if request.ingestion_job_id:
        try:
            ingestion_job_id = uuid.UUID(request.ingestion_job_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid ingestion_job_id")

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
                ingestion_job_id=ingestion_job_id,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "ingestion_job_id": request.ingestion_job_id,
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
                "merged_fingerprints": c.merged_fingerprints,
            }
            for c in clusters
        ],
    }
