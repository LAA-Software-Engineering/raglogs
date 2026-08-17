"""POST /query/compare — same pipeline as `raglogs compare`."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.api.schemas.v1 import CompareResponse
from src.core.compare.differ import CompareResult

router = APIRouter()


class CompareRequest(BaseModel):
    since: Optional[str] = None
    baseline: Optional[str] = None
    window_a_from: Optional[datetime] = None
    window_a_to: Optional[datetime] = None
    window_b_from: Optional[datetime] = None
    window_b_to: Optional[datetime] = None
    service: Optional[str] = None
    env: Optional[str] = None
    all_ingestions: bool = False
    ingestion_job_id: Optional[str] = None
    format: Literal["json", "text"] = "json"


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_compare_windows(request: CompareRequest) -> tuple[datetime, datetime, datetime, datetime]:
    from src.utils.time import parse_duration

    now = datetime.now(tz=timezone.utc)

    if request.since and request.baseline:
        incident_delta = parse_duration(request.since)
        baseline_delta = parse_duration(request.baseline)
        a_end = now
        a_start = now - incident_delta
        b_end = now - baseline_delta
        b_start = b_end - incident_delta
        return a_start, a_end, b_start, b_end

    if (
        request.window_a_from
        and request.window_a_to
        and request.window_b_from
        and request.window_b_to
    ):
        return (
            _ensure_utc(request.window_a_from),
            _ensure_utc(request.window_a_to),
            _ensure_utc(request.window_b_from),
            _ensure_utc(request.window_b_to),
        )

    raise ValueError(
        "Provide either since+baseline, or window_a_from/to and window_b_from/to"
    )


def _compare_result_to_response(result: CompareResult, *, scope: str) -> CompareResponse:
    from src.api.schemas.v1 import (
        SCHEMA_VERSION,
        cluster_diff_model,
        llm_rules_only,
        trigger_diff_model,
        window_from_bounds,
    )

    return CompareResponse(
        schema_version=SCHEMA_VERSION,
        scope=scope,
        window_a=window_from_bounds(result.window_a_start, result.window_a_end),
        window_b=window_from_bounds(result.window_b_start, result.window_b_end),
        has_changes=result.has_changes,
        new_clusters=[cluster_diff_model(d, "new") for d in result.new_clusters],
        disappeared_clusters=[cluster_diff_model(d, "disappeared") for d in result.disappeared_clusters],
        increased_clusters=[cluster_diff_model(d, "increased") for d in result.increased_clusters],
        decreased_clusters=[cluster_diff_model(d, "decreased") for d in result.decreased_clusters],
        new_triggers=[trigger_diff_model(t) for t in result.new_triggers],
        dropped_triggers=[trigger_diff_model(t) for t in result.dropped_triggers],
        llm=llm_rules_only(),
    )


def _trunc(text: str, n: int = 72) -> str:
    if len(text) <= n:
        return text
    cut = text[:n]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n // 2 else cut) + "…"


def _format_counts(count_a: Optional[int], count_b: Optional[int]) -> str:
    if count_a is not None and count_b is None:
        return f"{count_a} events"
    if count_b is not None and count_a is None:
        return f"{count_b} events"
    if count_a is not None and count_b is not None:
        return f"{count_b} → {count_a}"
    return ""


def format_compare_plain(result: CompareResult) -> str:
    """Plain-text layout matching CLI `compare` (no Rich)."""
    from src.utils.time import format_window

    lines: list[str] = []
    lines.append("")
    lines.append("Incident comparison")
    lines.append("")
    lines.append(f"  Window A (now):      {format_window(result.window_a_start, result.window_a_end)}")
    lines.append(f"  Window B (baseline): {format_window(result.window_b_start, result.window_b_end)}")
    lines.append("")

    if not result.has_changes:
        lines.append("  No significant changes between windows.")
        lines.append("")
        return "\n".join(lines)

    def row(sigil: str, msg: str, count_a: Optional[int], count_b: Optional[int]) -> None:
        count_str = _format_counts(count_a, count_b)
        lines.append(f"  {sigil} {_trunc(msg):<74} {count_str}".rstrip())

    if result.new_clusters:
        lines.append("New error clusters")
        for d in result.new_clusters:
            row("+", d.message, d.count_a, d.count_b)
        lines.append("")

    if result.disappeared_clusters:
        lines.append("Errors that disappeared")
        for d in result.disappeared_clusters:
            row("-", d.message, d.count_a, d.count_b)
        lines.append("")

    if result.increased_clusters:
        lines.append("Errors that increased")
        for d in result.increased_clusters:
            row("↑", d.message, d.count_a, d.count_b)
        lines.append("")

    if result.decreased_clusters:
        lines.append("Errors that decreased")
        for d in result.decreased_clusters:
            row("↓", d.message, d.count_a, d.count_b)
        lines.append("")

    if result.new_triggers:
        lines.append("Triggers in A not seen in B")
        for t in result.new_triggers:
            svc = f" · {t.service}" if t.service else ""
            lines.append(f"  +⚡ {_trunc(t.message)}{svc}")
        lines.append("")

    if result.dropped_triggers:
        lines.append("Triggers in B not seen in A")
        for t in result.dropped_triggers:
            svc = f" · {t.service}" if t.service else ""
            lines.append(f"  -⚡ {_trunc(t.message)}{svc}")
        lines.append("")

    return "\n".join(lines)


@router.post(
    "/compare",
    response_model=CompareResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
)
def compare_endpoint(request: CompareRequest, http_request: Request) -> CompareResponse:
    from src.api.schemas.v1 import scope_from_request
    from src.core.clustering.clusterer import run_clustering
    from src.core.compare.differ import compare_windows as run_compare_windows
    from src.core.explain.evidence import assemble_evidence
    from src.core.explain.summarizer import get_latest_ingestion_job_id
    from src.db.session import get_db

    try:
        a_start, a_end, b_start, b_end = _resolve_compare_windows(request)
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
            job_id = None
            if ingestion_job_id is not None:
                job_id = ingestion_job_id
            elif not request.all_ingestions:
                job_id = get_latest_ingestion_job_id(db)

            _, clusters_a = run_clustering(
                db=db,
                window_start=a_start,
                window_end=a_end,
                service=request.service,
                environment=request.env,
                save_to_db=False,
                ingestion_job_id=job_id,
                max_clusters=50,
            )
            _, clusters_b = run_clustering(
                db=db,
                window_start=b_start,
                window_end=b_end,
                service=request.service,
                environment=request.env,
                save_to_db=False,
                ingestion_job_id=job_id,
                max_clusters=50,
            )

            packet_a = assemble_evidence(
                db=db,
                window_start=a_start,
                window_end=a_end,
                clusters=clusters_a,
                service_filter=request.service,
                environment_filter=request.env,
                ingestion_job_id=job_id,
            )
            packet_b = assemble_evidence(
                db=db,
                window_start=b_start,
                window_end=b_end,
                clusters=clusters_b,
                service_filter=request.service,
                environment_filter=request.env,
                ingestion_job_id=job_id,
            )

            result = run_compare_windows(
                clusters_a=clusters_a,
                clusters_b=clusters_b,
                triggers_a=packet_a.trigger_candidates,
                triggers_b=packet_b.trigger_candidates,
                window_a_start=a_start,
                window_a_end=a_end,
                window_b_start=b_start,
                window_b_end=b_end,
            )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    body = _compare_result_to_response(result, scope=scope_from_request(http_request))
    if request.format == "text":
        rendered = format_compare_plain(result)
        body = body.model_copy(update={"rendered_text": rendered, "text": rendered})
    return body
