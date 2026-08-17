"""Similar-incident search API (G11).

POST /query/similar — given a scope's primary cluster(s), return prior
incidents with nearby fingerprints. Dual-mounted at /v1/query/similar and
the deprecated /query/similar alias.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.schemas.v1 import (
    SCHEMA_VERSION,
    SimilarMatchModel,
    SimilarQueryCluster,
    SimilarResponse,
    llm_rules_only,
    window_from_bounds,
)

router = APIRouter()


class SimilarRequest(BaseModel):
    since: Optional[str] = None
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    service: Optional[str] = None
    env: Optional[str] = None
    ingestion_job_id: Optional[str] = None
    fingerprint: Optional[str] = None
    fingerprints: Optional[list[str]] = None
    top: int = Field(default=10, ge=1, le=50)
    cross_scope: Optional[bool] = None
    scope: Optional[str] = None


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


@router.post(
    "/similar",
    response_model=SimilarResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
)
def similar_endpoint(request: SimilarRequest, http_request: Request) -> SimilarResponse:
    from src.api.auth.middleware import AuthPrincipal
    from src.api.auth.scope import bind_request_scope
    from src.config import get_settings
    from src.core.clustering.clusterer import run_clustering
    from src.core.retrieval.similar import (
        collect_query_fingerprints,
        find_similar_incidents,
        query_clusters_from_cluster_data,
        query_clusters_from_fingerprints,
        render_similar_summary,
        resolve_similar_visibility,
    )
    from src.db.session import get_db
    from src.utils.time import resolve_window

    scope = bind_request_scope(http_request, request.scope)
    settings = get_settings()
    principal = getattr(http_request.state, "auth_principal", None)
    if principal is not None and not isinstance(principal, AuthPrincipal):
        principal = None

    visibility = resolve_similar_visibility(
        auth_enabled=bool(settings.auth_enabled),
        resolved_scope=scope,
        cross_scope_requested=request.cross_scope,
        role=principal.role if principal is not None else "",
        allow_scope_override=bool(principal.allow_scope_override)
        if principal is not None
        else False,
    )

    fingerprints = collect_query_fingerprints(request.fingerprint, request.fingerprints)

    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    try:
        window_start, window_end = resolve_window(
            since=request.since,
            from_time=request.from_time,
            to_time=request.to_time,
        )
    except ValueError as exc:
        if not fingerprints:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        now = datetime.now(tz=timezone.utc)
        window_start, window_end = now, now

    ingestion_job_id: Optional[uuid.UUID] = None
    if request.ingestion_job_id:
        try:
            ingestion_job_id = uuid.UUID(request.ingestion_job_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid ingestion_job_id"
            ) from exc

    try:
        with get_db() as db:
            if fingerprints:
                query_clusters = query_clusters_from_fingerprints(fingerprints)
            else:
                _, clusters = run_clustering(
                    db=db,
                    window_start=window_start,
                    window_end=window_end,
                    service=request.service,
                    environment=request.env,
                    max_clusters=max(request.top, 5),
                    save_to_db=False,
                    ingestion_job_id=ingestion_job_id,
                    scope=scope,
                )
                query_clusters = query_clusters_from_cluster_data(clusters)

            result = find_similar_incidents(
                db,
                query_clusters,
                query_scope=scope,
                visibility=visibility,
                window_start=window_start,
                window_end=window_end,
                top=request.top,
            )
    except Exception:
        from src.core.retrieval.similar import SimilarResult

        result = SimilarResult(
            query_clusters=query_clusters_from_fingerprints(fingerprints),
            matches=[],
            retrieval_mode="fingerprint",
            window_start=window_start,
            window_end=window_end,
        )

    return SimilarResponse(
        schema_version=SCHEMA_VERSION,
        scope=scope,
        window=window_from_bounds(result.window_start, result.window_end),
        query_clusters=[
            SimilarQueryCluster(fingerprint=c.fingerprint, template=c.template or None)
            for c in result.query_clusters
        ],
        matches=[
            SimilarMatchModel(
                scope=m.scope,
                fingerprint=m.fingerprint,
                template=m.template,
                similarity=round(m.similarity, 4),
                first_seen=_iso(m.first_seen),
                last_seen=_iso(m.last_seen),
                count=m.count,
            )
            for m in result.matches
        ],
        retrieval_mode=result.retrieval_mode,
        llm=llm_rules_only(),
        rendered_text=render_similar_summary(result.matches, result.retrieval_mode),
    )
