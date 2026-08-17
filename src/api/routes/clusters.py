"""Clusters API route with optional ingestion_job_id scoping."""
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.api.overrides import QueryOverrideFields, override_input_from_request
from src.api.schemas.v1 import (
    SCHEMA_VERSION,
    ClustersResponse,
    llm_from_overrides,
    window_from_bounds,
)

router = APIRouter()


class ClustersRequest(QueryOverrideFields):
    since: Optional[str] = None
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    service: Optional[str] = None
    env: Optional[str] = None
    top: Optional[int] = None
    ingestion_job_id: Optional[str] = None
    scope: Optional[str] = None


@router.post(
    "/clusters",
    response_model=ClustersResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
)
def clusters_endpoint(request: ClustersRequest, http_request: Request) -> ClustersResponse:
    from dataclasses import replace

    from src.api.auth.middleware import AuthPrincipal
    from src.api.auth.scope import bind_request_scope
    from src.api.overrides import resolve_query_overrides
    from src.config import get_settings
    from src.core.clustering.clusterer import run_clustering
    from src.db.session import get_db
    from src.utils.time import resolve_window

    scope = bind_request_scope(http_request, request.scope)
    settings = get_settings()
    principal = getattr(http_request.state, "auth_principal", None)
    if not isinstance(principal, AuthPrincipal):
        principal = None
    fields = override_input_from_request(request)
    if fields.max_clusters is None and request.top is not None:
        fields = replace(fields, max_clusters=request.top)
    overrides = resolve_query_overrides(
        fields,
        principal if settings.auth_enabled else None,
        settings,
        auth_enabled=bool(settings.auth_enabled),
    )

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
                baseline_window_str=overrides.baseline_window,
                max_clusters=overrides.max_clusters,
                save_to_db=False,
                ingestion_job_id=ingestion_job_id,
                scope=scope,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return ClustersResponse(
        schema_version=SCHEMA_VERSION,
        scope=scope,
        window=window_from_bounds(window_start, window_end),
        ingestion_job_id=request.ingestion_job_id,
        llm=llm_from_overrides(
            mode="rules",
            llm_provider=overrides.llm_provider,
            llm_enabled=False,
        ),
        clusters=[
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
    )
