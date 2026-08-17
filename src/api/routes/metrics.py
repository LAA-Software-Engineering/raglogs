"""Prometheus scrape endpoint — unauthenticated, no rate limit."""

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.observability.metrics import REGISTRY, refresh_runtime_gauges

router = APIRouter()


@router.get("/metrics")
def prometheus_metrics() -> Response:
    """Expose Prometheus text format. Exempt from auth (G2) and rate limits."""
    refresh_runtime_gauges()
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
