import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy.orm import Session

from src.config import get_settings
from src.core.clustering.clusterer import run_clustering
from src.core.explain.confidence import compute_confidence
from src.core.explain.evidence import EvidencePacket, assemble_evidence
from src.core.explain.templates import render_insufficient_evidence, render_text_summary
from src.core.llm.provider import build_llm_provider
from src.db.models import DEFAULT_LOG_SCOPE, IngestionJob
from src.db.scope_filter import filter_ingestion_jobs_by_scope

log = structlog.get_logger()


@dataclass
class ExplainResult:
    window_start: datetime
    window_end: datetime
    summary_text: str
    confidence: str
    evidence_items: list[str]
    services_affected: list[str]
    primary_cluster: Optional[dict] = None
    secondary_clusters: list[dict] = field(default_factory=list)
    trigger_candidates: list[dict] = field(default_factory=list)
    total_logs: int = 0
    mode: str = "rules"


def get_latest_ingestion_job_id(
    db: Session,
    scope: str = DEFAULT_LOG_SCOPE,
) -> Optional[uuid.UUID]:
    """Return the ID of the most recently completed ingestion job in ``scope``."""
    from sqlalchemy import desc, select

    stmt = (
        select(IngestionJob)
        .where(IngestionJob.status == "completed")
        .order_by(desc(IngestionJob.finished_at))
        .limit(1)
    )
    stmt = filter_ingestion_jobs_by_scope(stmt, scope)
    job = db.execute(stmt).scalar_one_or_none()
    return job.id if job else None


def explain_window(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    service: Optional[str] = None,
    environment: Optional[str] = None,
    no_llm: bool = False,
    max_clusters: int = 10,
    baseline_window_str: Optional[str] = None,
    ingestion_job_id: Optional[uuid.UUID] = None,
    scope: str = DEFAULT_LOG_SCOPE,
) -> ExplainResult:
    """
    Full explain pipeline for a time window.
    """
    from src.observability.tracing import start_span

    with start_span("explain", **{"raglogs.scope": scope}):
        return _explain_window(
            db=db,
            window_start=window_start,
            window_end=window_end,
            service=service,
            environment=environment,
            no_llm=no_llm,
            max_clusters=max_clusters,
            baseline_window_str=baseline_window_str,
            ingestion_job_id=ingestion_job_id,
            scope=scope,
        )


def _explain_window(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    service: Optional[str] = None,
    environment: Optional[str] = None,
    no_llm: bool = False,
    max_clusters: int = 10,
    baseline_window_str: Optional[str] = None,
    ingestion_job_id: Optional[uuid.UUID] = None,
    scope: str = DEFAULT_LOG_SCOPE,
) -> ExplainResult:
    """
    Full explain pipeline for a time window.
    """
    settings = get_settings()
    baseline_window = baseline_window_str or settings.default_baseline_window

    # 1. Cluster
    _, clusters = run_clustering(
        db=db,
        window_start=window_start,
        window_end=window_end,
        service=service,
        environment=environment,
        baseline_window_str=baseline_window,
        max_clusters=max_clusters,
        save_to_db=True,
        ingestion_job_id=ingestion_job_id,
        scope=scope,
    )

    # 2. Assemble evidence
    packet = assemble_evidence(
        db=db,
        window_start=window_start,
        window_end=window_end,
        clusters=clusters,
        service_filter=service,
        environment_filter=environment,
        max_evidence_items=settings.max_evidence_items,
        ingestion_job_id=ingestion_job_id,
        scope=scope,
    )

    # 3. Confidence
    confidence = compute_confidence(packet)

    # 4. Handle empty case
    if not clusters or packet.primary_cluster is None:
        return ExplainResult(
            window_start=window_start,
            window_end=window_end,
            summary_text=render_insufficient_evidence(window_start, window_end, packet.total_logs),
            confidence="low",
            evidence_items=packet.evidence_items,
            services_affected=packet.services_affected,
            total_logs=packet.total_logs,
            mode="rules",
        )

    # 5. Generate summary
    mode = "rules"
    summary_text = ""
    llm_requested = not no_llm and settings.llm_provider != "disabled"

    if llm_requested:
        try:
            llm = build_llm_provider(settings)
            evidence_dict = _packet_to_dict(packet)
            llm_text = llm.generate_summary(evidence_dict)
            if llm_text:
                summary_text = llm_text
                mode = "llm"
        except Exception:
            # Timeout, retries exhausted, open breaker, or budget: keep mode
            # "rules" so llm.fell_back is true when an LLM was requested.
            log.warning("llm_explain_failed", exc_info=True)

    if llm_requested and mode != "llm":
        from src.observability.metrics import record_llm_fallback

        record_llm_fallback()

    if not summary_text:
        summary_text = render_text_summary(packet, confidence)

    pc = packet.primary_cluster
    return ExplainResult(
        window_start=window_start,
        window_end=window_end,
        summary_text=summary_text,
        confidence=confidence,
        evidence_items=packet.evidence_items,
        services_affected=packet.services_affected,
        total_logs=packet.total_logs,
        mode=mode,
        primary_cluster={
            "message": pc.representative_message,
            "count": pc.count,
            "services": list(pc.services.keys()),
            "levels": list(pc.levels.keys()),
            "fingerprint": pc.fingerprint,
            "importance_score": round(pc.importance_score, 2),
            "first_seen": pc.first_seen.isoformat() if pc.first_seen else None,
            "last_seen": pc.last_seen.isoformat() if pc.last_seen else None,
            "baseline_count": pc.baseline_count,
            "change_ratio": round(pc.change_ratio, 2),
        } if pc else None,
        secondary_clusters=[
            {
                "message": c.representative_message,
                "count": c.count,
                "services": list(c.services.keys()),
                "levels": list(c.levels.keys()),
                "fingerprint": c.fingerprint,
                "importance_score": round(c.importance_score, 2),
                "first_seen": c.first_seen.isoformat() if c.first_seen else None,
                "last_seen": c.last_seen.isoformat() if c.last_seen else None,
                "baseline_count": c.baseline_count,
                "change_ratio": round(c.change_ratio, 2),
            }
            for c in packet.secondary_clusters
        ],
        trigger_candidates=[
            {
                "message": t.message,
                "timestamp": t.timestamp.isoformat() if t.timestamp else None,
                "service": t.service,
            }
            for t in packet.trigger_candidates
        ],
    )


def _packet_to_dict(packet: EvidencePacket) -> dict:
    pc = packet.primary_cluster
    return {
        "window": {
            "start": packet.window_start.isoformat(),
            "end": packet.window_end.isoformat(),
        },
        "total_logs": packet.total_logs,
        "primary_cluster": {
            "message": pc.representative_message if pc else None,
            "count": pc.count if pc else 0,
            "services": list(pc.services.keys()) if pc else [],
            "first_seen": pc.first_seen.isoformat() if pc and pc.first_seen else None,
            "baseline_count": pc.baseline_count if pc else 0,
            "change_ratio": round(pc.change_ratio, 2) if pc else 0,
        } if pc else None,
        "secondary_clusters": [
            {"message": c.representative_message, "count": c.count, "services": list(c.services.keys())}
            for c in packet.secondary_clusters
        ],
        "trigger_candidates": [
            {"message": t.message, "timestamp": t.timestamp.isoformat() if t.timestamp else None, "service": t.service}
            for t in packet.trigger_candidates
        ],
        "evidence": packet.evidence_items,
        "services_affected": packet.services_affected,
    }
