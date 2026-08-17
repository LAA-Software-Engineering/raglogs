import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from src.config import get_settings
from src.core.clustering.clusterer import run_clustering
from src.db.models import IngestionJob
from src.core.explain.confidence import compute_confidence
from src.core.explain.evidence import EvidencePacket, assemble_evidence
from src.core.explain.templates import render_insufficient_evidence, render_text_summary
from src.core.llm.provider import NoopLLMProvider, build_llm_provider
from src.utils.time import resolve_baseline_window


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


def get_latest_ingestion_job_id(db: Session) -> Optional[uuid.UUID]:
    """Return the ID of the most recently completed ingestion job."""
    from sqlalchemy import select, desc
    job = db.execute(
        select(IngestionJob).where(IngestionJob.status == "completed").order_by(desc(IngestionJob.finished_at)).limit(1)
    ).scalar_one_or_none()
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

    if not no_llm and settings.llm_provider != "disabled":
        try:
            llm = build_llm_provider(settings)
            if not isinstance(llm, NoopLLMProvider):
                evidence_dict = _packet_to_dict(packet)
                llm_text = llm.generate_summary(evidence_dict)
                if llm_text:
                    summary_text = llm_text
                    mode = "llm"
        except Exception as e:
            # Degrade gracefully
            pass

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
                "fingerprint": c.fingerprint,
                "importance_score": round(c.importance_score, 2),
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
