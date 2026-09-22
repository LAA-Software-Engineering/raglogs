import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy.orm import Session

from src.config import get_settings
from src.core.clustering.clusterer import run_clustering
from src.core.explain.confidence import compute_confidence, label_from_calibrated_probability
from src.core.explain.evidence import EvidencePacket, assemble_evidence
from src.core.explain.templates import render_insufficient_evidence, render_text_summary
from src.core.llm.provider import build_llm_provider
from src.db.models import DEFAULT_LOG_SCOPE, IngestionJob
from src.db.scope_filter import filter_ingestion_jobs_by_scope

log = structlog.get_logger()


def _ordered_services(cluster) -> list[str]:
    """A cluster's services, most-implicated first.

    A fingerprint can span services, so the services dict is in arbitrary (DB
    row) order, yet consumers read ``services[0]`` as the cluster's service. The
    trivial baseline attributes root cause to the service with the most
    error/fatal lines of a (service, fingerprint) group, so order by that first
    (``error_service_counts``), then by total volume — dominant service first
    (#82).
    """
    esc = getattr(cluster, "error_service_counts", {}) or {}
    return sorted(
        cluster.services,
        key=lambda s: (esc.get(s, 0), cluster.services.get(s, 0)),
        reverse=True,
    )


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
    # Learned multi-modal RCA ranker output (#118 C2), populated only when a model
    # artifact is configured (settings.rca_ranker_model_path). Empty otherwise, so
    # the log-cluster path is unchanged when no model is present.
    predicted_root_cause: Optional[str] = None
    root_cause_candidates: list[dict] = field(default_factory=list)
    # The full eligible candidate set of the ACTIVE generation mechanism: the learned ranker's
    # post-exclusion, pre-top-k list when it produced candidates, otherwise the legacy
    # significant-cluster pool (never a union of the two, and never `services_affected`, which
    # includes informational-only / excluded services that were never eligible). Distinct from the
    # selected/top-k output above: eval's failure taxonomy uses it to tell "cause never generated"
    # (coverage) from "generated but not selected/ranked" (inference). Instrumentation only.
    generated_candidates: list[str] = field(default_factory=list)
    # Absence-derived candidates (#184 Phase F): services whose span traffic was baselined then
    # collapsed in the incident — silent failures the incident-window features can't see. Unioned into
    # `generated_candidates` so a vanished root cause is generated regardless of the learned ranker.
    absence_candidates: list[str] = field(default_factory=list)
    # Calibrated P(top-1 correct) for the ranker prediction (#118 D / #83). Set
    # only when a calibrator model is configured; None otherwise.
    predicted_root_cause_confidence: Optional[float] = None
    # True when the `confidence` label was bucketed from the calibrated probability
    # (#83) — i.e. `confidence` means P(root-cause service correct). False on the
    # legacy ordinal path and the insufficient-evidence case (where the label stays
    # "low" for narrative coherence even if the ranker was confident).
    confidence_calibrated: bool = False


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
    max_evidence_items: Optional[int] = None,
    llm_provider: Optional[str] = None,
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
            max_evidence_items=max_evidence_items,
            llm_provider=llm_provider,
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
    max_evidence_items: Optional[int] = None,
    llm_provider: Optional[str] = None,
) -> ExplainResult:
    """
    Full explain pipeline for a time window.
    """
    settings = get_settings()
    if llm_provider is not None or max_evidence_items is not None:
        update: dict[str, object] = {}
        if llm_provider is not None:
            update["llm_provider"] = llm_provider
        if max_evidence_items is not None:
            update["max_evidence_items"] = max_evidence_items
        settings = settings.model_copy(update=update)
    baseline_window = baseline_window_str or settings.default_baseline_window
    evidence_cap = (
        max_evidence_items if max_evidence_items is not None else settings.max_evidence_items
    )

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
        max_evidence_items=evidence_cap,
        ingestion_job_id=ingestion_job_id,
        scope=scope,
    )

    # 3. Confidence
    confidence = compute_confidence(packet)

    # 3.5 Learned multi-modal RCA ranker (#118 C2). When a model artifact is
    # configured it ranks candidate services across logs/traces/metrics — this can
    # localise trace/metric-only root causes that have no distinctive log cluster.
    # With no model this is skipped entirely and the log-cluster path is unchanged.
    predicted_root_cause, rca_candidates, rca_confidence, ranked_services = _rank_candidates(
        db, scope, window_start, window_end, baseline_window, settings
    )
    # The generated-candidate set of the ACTIVE mechanism (not a union of both): when the learned
    # ranker produced candidates it *is* the generator (its full post-exclusion, pre-top-k list),
    # otherwise the legacy significant-cluster pool. Never `services_affected` — that includes
    # informational-only and excluded services that were never eligible candidates.
    generated_candidates = sorted(ranked_services) if ranked_services else list(packet.candidate_services)
    # Phase F (#184, evidence-only): services whose span traffic was baselined then collapsed. This is
    # observed **evidence** ("went silent"), NOT a causal candidate — traces alone cannot attribute the
    # silence to a failed call toward the service without per-edge telemetry (unreachable vs a
    # trace-collection gap are indistinguishable). So it is surfaced as an evidence item and its own
    # `absence_candidates` field ONLY, and is deliberately NOT unioned into `generated_candidates`,
    # `root_cause_candidates`, or coverage — it must not affect causal prediction/ranking/scoring.
    absence_candidates = _silent_services(db, scope, window_start, window_end, baseline_window)
    evidence_items = list(packet.evidence_items) + (
        [f"{len(absence_candidates)} service(s) went silent in traces (span traffic collapsed vs "
         f"baseline — unreachable or a trace-collection gap): " + ", ".join(absence_candidates)]
        if absence_candidates else []
    )

    # NOTE: a metric/log abstention "gate" (#79) was investigated and shelved as a
    # negative result — no modality on the available dev corpora both calibrates and
    # transfers (metrics saturate on real OTLP counters; logs can't separate healthy
    # from resource incidents at high recall). See docs/eval-abstention.md. The
    # existing "insufficient evidence" case below (no significant clusters) stands.

    # 4. Handle empty case. The insufficient-evidence narrative keeps a "low"
    # label even if a metric/trace-only ranker was confident — the calibrated
    # probability still rides on predicted_root_cause_confidence, but overriding
    # the label here would pair "insufficient evidence" prose with a "high" badge.
    if not clusters or packet.primary_cluster is None:
        return ExplainResult(
            window_start=window_start,
            window_end=window_end,
            summary_text=render_insufficient_evidence(window_start, window_end, packet.total_logs),
            confidence="low",
            evidence_items=evidence_items,
            services_affected=packet.services_affected,
            total_logs=packet.total_logs,
            mode="rules",
            predicted_root_cause=predicted_root_cause,
            root_cause_candidates=rca_candidates,
            generated_candidates=generated_candidates,
            absence_candidates=absence_candidates,
            predicted_root_cause_confidence=rca_confidence,
        )

    # #83: with a real explanation, when a ranker + calibrator produced a
    # calibrated P(top-1 root-cause correct), the confidence LABEL is bucketed from
    # that probability instead of the (uncalibrated) ordinal points. It then means
    # "confidence the predicted root-cause service is correct", not the whole
    # narrative. No calibrator -> rca_confidence is None -> legacy ordinal label.
    confidence_calibrated = rca_confidence is not None
    if confidence_calibrated:
        confidence = label_from_calibrated_probability(rca_confidence)

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
        evidence_items=evidence_items,
        services_affected=packet.services_affected,
        total_logs=packet.total_logs,
        mode=mode,
        primary_cluster={
            "message": pc.representative_message,
            "count": pc.count,
            "services": _ordered_services(pc),
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
                "services": _ordered_services(c),
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
        predicted_root_cause=predicted_root_cause,
        root_cause_candidates=rca_candidates,
        generated_candidates=generated_candidates,
        absence_candidates=absence_candidates,
        predicted_root_cause_confidence=rca_confidence,
        confidence_calibrated=confidence_calibrated,
    )


def _silent_services(
    db: Session, scope: str, window_start: datetime, window_end: datetime, baseline_window: str
) -> list[str]:
    """Services whose span traffic was baselined then collapsed in the incident (#184 Phase F,
    evidence-only). Returns them for the "went silent in traces" **evidence item** — a diagnostic clue,
    never a causal candidate: silence can't be attributed to a failed call toward the service without
    per-edge telemetry, so this must not feed candidate generation, ranking, or coverage scoring.

    Two per-service **aggregates** (baseline counts, incident counts) — no span ORM rows / JSON
    attributes are loaded into Python. Honest cost note: each ``GROUP BY service`` still scans the
    spans in its ``(scope, time-range)`` at the DB (index-supported range scan, but O(spans in the
    window)); this is acceptable at current scale and the scale path is maintained per-service
    time-bucket rollups rather than recomputing from raw spans on every explain."""
    from sqlalchemy import func, select

    from src.core.rca.features import collapsed_services
    from src.db.models import TraceSpan
    from src.utils.time import parse_duration

    try:
        baseline_start = window_start - parse_duration(baseline_window)
    except (ValueError, TypeError):
        return []
    baseline_seconds = max((window_start - baseline_start).total_seconds(), 1.0)
    incident_seconds = max((window_end - window_start).total_seconds(), 1.0)
    svc = TraceSpan.service

    def _counts(lo: datetime, hi: datetime) -> dict[str, int]:
        return {
            s: n
            for s, n in db.execute(
                select(svc, func.count())
                .where(TraceSpan.scope == scope, svc.isnot(None),
                       TraceSpan.start_time >= lo, TraceSpan.start_time < hi)
                .group_by(svc)
            ).all()
        }

    baseline_counts = _counts(baseline_start, window_start)
    if not baseline_counts:  # no baseline traces -> nothing could have gone silent
        return []
    # incident window is inclusive of window_end; nudge past it for the half-open helper.
    incident_counts = _counts(window_start, window_end + timedelta(microseconds=1))
    return sorted(
        collapsed_services(baseline_counts, incident_counts, baseline_seconds, incident_seconds)
    )


def _rank_candidates(
    db: Session,
    scope: str,
    window_start: datetime,
    window_end: datetime,
    baseline_window: str,
    settings,
    top_k: int = 5,
) -> tuple[Optional[str], list[dict], Optional[float], list[str]]:
    """Rank candidate services with the learned ranker. Returns
    ``(top_service, top_k_candidate_dicts, calibrated_confidence, all_candidate_services)`` — the last
    being the FULL post-exclusion, pre-top-k candidate list (the generation boundary). All four are
    empty/``None`` (``(None, [], None, [])``) when no model artifact is configured — a graceful
    fallback, and the caller then relies on the legacy log-cluster candidate pool."""
    from src.core.rca.ranker import load_ranker

    ranker = load_ranker(settings.rca_ranker_model_path)
    if ranker is None:
        return None, [], None, []

    from src.core.rca.candidates import build_candidates
    from src.core.rca.features import compute_features
    from src.utils.time import parse_duration

    try:
        baseline_start = window_start - parse_duration(baseline_window)
    except (ValueError, TypeError):
        baseline_start = window_start
    table = compute_features(
        db,
        scope,
        incident_start=window_start,
        incident_end=window_end,
        baseline_start=baseline_start,
    )
    excluded = frozenset(
        s.strip() for s in (get_settings().rca_excluded_services or "").split(",") if s.strip()
    )
    candidates = build_candidates(table, scorer=ranker.score, exclude=excluded)
    if not candidates:
        return None, [], None, []
    ranker_top = candidates[0].service
    # Trace-graph propagation rerank (#118 / #79 carve-out, opt-in). Refines the
    # order so a true upstream culprit can overtake the loud caller that only
    # carries the downstream symptom. No-op without traces / onset data.
    if settings.rca_propagation_rerank:
        candidates = _propagation_rerank(db, scope, candidates, window_start, window_end)
    # Calibrated P(top-1 correct) — only when a calibrator model is configured. The
    # calibrator was fit on the *ranker's* top-1 score, so it is only valid when the
    # reranker left top-1 unchanged. When the reranker promotes a different service
    # (whose original ranker score no longer reflects the decision rule), the calibrated
    # probability would be semantically false — withhold it (falls back to the ordinal
    # label) until the combined ranker+reranker policy is calibrated on real trace data.
    from src.core.rca.calibration import calibrated_confidence, load_calibrator

    calibrator = load_calibrator(settings.rca_calibrator_model_path)
    rerank_changed_top1 = candidates[0].service != ranker_top
    if calibrator is not None and not rerank_changed_top1:
        confidence = calibrated_confidence(calibrator, candidates)
    else:
        confidence = None
    # Also return the FULL ranked service list (untruncated) so the caller can record the complete
    # generated-candidate set — a labeled cause ranked beyond top-k is "generated", not "missing".
    ranked_services = [c.service for c in candidates]
    return candidates[0].service, [c.to_dict() for c in candidates[:top_k]], confidence, ranked_services


def _propagation_rerank(
    db: Session,
    scope: str,
    candidates: list,
    window_start: datetime,
    window_end: datetime,
) -> list:
    """Reorder candidates with two trace-graph signals (#118). Builds the caller->callee
    graph, then:

    - the log-onset term (RCAEval): early-degrading log-erroring service = upstream cause;
    - the symptom-anchor dependency term (real-OTel pivot): trace ERROR-status services are
      *symptoms*, not causes — on real traces the failing service rarely marks its own span
      ERROR, its callers do — so we walk from those anchors to their common **dependency**
      and boost it (docs/eval-trace-localization.md).

    A no-op (original order) when there are no traces."""
    from src.core.rca.linkage import build_service_graph
    from src.core.rca.propagation import (
        combine_log_trace_evidence,
        failed_edge_dependencies,
        rerank_candidates,
    )

    graph = build_service_graph(db, scope, window_start, window_end)
    if graph.empty:
        return candidates
    log_onset = _error_onsets(db, scope, window_start, window_end)
    log_err = {c.service: float(getattr(c.features, "log_err", 0.0)) for c in candidates}
    # Log evidence drives the onset term (RCAEval); trace ERROR-status is a symptom anchor,
    # not a cause, so it feeds the failed-edge dependency term instead.
    onset, anomaly = combine_log_trace_evidence(log_onset, log_err, {})
    dependency = failed_edge_dependencies(_error_span_edges(db, scope, window_start, window_end))
    return rerank_candidates(
        candidates, graph, onset, anomaly=anomaly, dependency_boost=dependency
    )


def _error_span_edges(db: Session, scope: str, window_start: datetime, window_end: datetime):
    """``(span_id, parent_span_id, service, status_code)`` for spans in the window — the
    input to :func:`failed_edge_dependencies` (error spans + their children)."""
    from sqlalchemy import select

    from src.db.models import TraceSpan

    rows = db.execute(
        select(TraceSpan.span_id, TraceSpan.parent_span_id, TraceSpan.service, TraceSpan.status_code)
        .where(
            TraceSpan.scope == scope,
            TraceSpan.start_time >= window_start,
            TraceSpan.start_time <= window_end,
        )
    ).all()
    return [(sid, pid, svc, sc) for sid, pid, svc, sc in rows]


def _error_onsets(
    db: Session, scope: str, window_start: datetime, window_end: datetime
) -> dict[str, float]:
    """Per-service onset: the earliest error/fatal log timestamp in the window, as
    epoch seconds (only onset *differences* matter to the reranker)."""
    from sqlalchemy import func, select

    from src.db.models import LogEntry
    from src.db.scope_filter import filter_log_entries_by_scope

    q = (
        select(LogEntry.service, func.min(LogEntry.timestamp))
        .where(
            LogEntry.timestamp >= window_start,
            LogEntry.timestamp <= window_end,
            func.lower(LogEntry.level).in_(("error", "fatal", "critical")),
            LogEntry.service.isnot(None),
        )
        .group_by(LogEntry.service)
    )
    q = filter_log_entries_by_scope(q, scope)
    return {svc: ts.timestamp() for svc, ts in db.execute(q).all() if svc and ts}


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
            "services": _ordered_services(pc) if pc else [],
            "first_seen": pc.first_seen.isoformat() if pc and pc.first_seen else None,
            "baseline_count": pc.baseline_count if pc else 0,
            "change_ratio": round(pc.change_ratio, 2) if pc else 0,
        } if pc else None,
        "secondary_clusters": [
            {"message": c.representative_message, "count": c.count, "services": _ordered_services(c)}
            for c in packet.secondary_clusters
        ],
        "trigger_candidates": [
            {"message": t.message, "timestamp": t.timestamp.isoformat() if t.timestamp else None, "service": t.service}
            for t in packet.trigger_candidates
        ],
        "evidence": packet.evidence_items,
        "services_affected": packet.services_affected,
    }
