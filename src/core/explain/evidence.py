import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Union

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.clustering.clusterer import ClusterData
from src.core.normalization.patterns import is_trigger_message
from src.db.models import DEFAULT_LOG_SCOPE, LogEntry
from src.db.scope_filter import filter_log_entries_by_scope
from src.utils.time import format_window


def _trunc(text: str, max_len: int) -> str:
    """Truncate at a word boundary."""
    if not text or len(text) <= max_len:
        return text or ""
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len // 2:
        truncated = truncated[:last_space]
    return truncated + "…"



@dataclass
class TriggerCandidate:
    message: str
    timestamp: datetime
    service: Optional[str]


@dataclass
class EvidencePacket:
    window_start: datetime
    window_end: datetime
    total_logs: int
    primary_cluster: Optional[ClusterData]
    secondary_clusters: list[ClusterData]
    trigger_candidates: list[TriggerCandidate]
    evidence_items: list[str]
    services_affected: list[str]
    service_filter: Optional[str] = None
    environment_filter: Optional[str] = None


def find_trigger_candidates(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    lookback_minutes: int = 10,
    ingestion_job_id: Optional[uuid.UUID] = None,
    scope: str = DEFAULT_LOG_SCOPE,
) -> list[TriggerCandidate]:
    """
    Find likely trigger events in a window slightly before the main window.
    """
    from datetime import timedelta

    search_start = window_start - timedelta(minutes=lookback_minutes)

    q = select(LogEntry).where(
        LogEntry.timestamp >= search_start,
        LogEntry.timestamp <= window_end,
    )
    q = filter_log_entries_by_scope(q, scope)
    if ingestion_job_id:
        q = q.where(LogEntry.ingestion_job_id == ingestion_job_id)
    q = q.limit(5000)

    rows = db.execute(q).scalars().all()

    candidates = []
    for entry in rows:
        # Prefer normalized_message; fall back to extracting message from raw JSON
        msg = entry.normalized_message or ""
        if not msg and entry.raw_message:
            try:
                import orjson
                parsed = orjson.loads(entry.raw_message)
                msg = parsed.get("message") or entry.raw_message
            except Exception:
                msg = entry.raw_message
        if is_trigger_message(msg):
            candidates.append(TriggerCandidate(
                message=msg[:500],
                timestamp=entry.timestamp,
                service=entry.service,
            ))

    # Sort by timestamp, deduplicate by message text
    candidates.sort(key=lambda c: c.timestamp or datetime.min.replace(tzinfo=timezone.utc))
    seen_msgs: set[str] = set()
    deduped: list[TriggerCandidate] = []
    for c in candidates:
        key = c.message[:100].strip()
        if key not in seen_msgs:
            seen_msgs.add(key)
            deduped.append(c)
    return deduped[:3]


def count_logs_in_window(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    service: Optional[str] = None,
    ingestion_job_id: Optional[uuid.UUID] = None,
    scope: str = DEFAULT_LOG_SCOPE,
) -> int:
    from sqlalchemy import func
    q = select(func.count(LogEntry.id)).where(
        LogEntry.timestamp >= window_start,
        LogEntry.timestamp <= window_end,
    )
    q = filter_log_entries_by_scope(q, scope)
    if service:
        q = q.where(LogEntry.service == service)
    if ingestion_job_id:
        q = q.where(LogEntry.ingestion_job_id == ingestion_job_id)
    result = db.execute(q).scalar()
    return result or 0


def assemble_evidence(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    clusters: list[ClusterData],
    service_filter: Optional[str] = None,
    environment_filter: Optional[str] = None,
    max_evidence_items: int = 8,
    ingestion_job_id: Optional[uuid.UUID] = None,
    scope: str = DEFAULT_LOG_SCOPE,
) -> EvidencePacket:
    """
    Assemble an evidence packet from clusters and window data.
    """
    total_logs = count_logs_in_window(
        db,
        window_start,
        window_end,
        service=service_filter,
        ingestion_job_id=ingestion_job_id,
        scope=scope,
    )

    # Error/warn clusters only for primary analysis
    significant_clusters = [
        c for c in clusters
        if any(l in ("error", "fatal", "warn", "critical") for l in c.levels)
    ]

    if not significant_clusters:
        significant_clusters = clusters

    primary = significant_clusters[0] if significant_clusters else None
    # Sort secondary by count descending — surface highest-volume effects first
    # Take from all remaining significant clusters, not just top-5 by importance
    secondary = sorted(significant_clusters[1:], key=lambda c: c.count, reverse=True)[:4] if len(significant_clusters) > 1 else []

    # Trigger candidates (look back up to 10 min before window start)
    triggers = find_trigger_candidates(
        db,
        window_start,
        window_end,
        lookback_minutes=10,
        ingestion_job_id=ingestion_job_id,
        scope=scope,
    )

    # Collect affected services
    services_set: set[str] = set()
    for c in clusters:
        services_set.update(c.services.keys())
    services_affected = sorted(services_set)

    # Build evidence items
    evidence_items = _build_evidence_items(
        primary=primary,
        secondary=secondary,
        triggers=triggers,
        total_logs=total_logs,
        window_start=window_start,
        max_items=max_evidence_items,
    )

    return EvidencePacket(
        window_start=window_start,
        window_end=window_end,
        total_logs=total_logs,
        primary_cluster=primary,
        secondary_clusters=secondary,
        trigger_candidates=triggers,
        evidence_items=evidence_items,
        services_affected=services_affected,
        service_filter=service_filter,
        environment_filter=environment_filter,
    )


def _build_evidence_items(
    primary: Optional[ClusterData],
    secondary: list[ClusterData],
    triggers: list[TriggerCandidate],
    total_logs: int,
    window_start: datetime,
    max_items: int = 8,
) -> list[str]:
    import re

    items: list[str] = []

    if primary is None:
        items.append(f"Total logs in window: {total_logs}")
        items.append("No significant error clusters detected")
        return items

    # Primary cluster — concise count + service
    svc_label = _services_str(primary)
    items.append(f"{primary.count} similar failures in {svc_label}")

    # Baseline signal
    if primary.baseline_count == 0:
        items.append("Not observed in prior 24h baseline")
    elif primary.change_ratio > 10:
        items.append(f"Count increased {primary.change_ratio:.0f}x vs baseline ({primary.baseline_count} prior events)")
    else:
        items.append(f"Baseline had {primary.baseline_count} similar events (change ratio: {primary.change_ratio:.1f}x)")

    # Timing relative to trigger
    if triggers and primary.first_seen:
        earliest_trigger = triggers[0]
        if earliest_trigger.timestamp and primary.first_seen:
            delta = primary.first_seen - earliest_trigger.timestamp
            minutes = int(delta.total_seconds() / 60)
            if 0 <= minutes <= 30:
                trigger_label = _trunc(earliest_trigger.message, 50)
                items.append(f"First error spike occurred {minutes}m after {trigger_label.lower()}")

    # Dominant endpoint
    if primary.representative_message:
        endpoint_match = re.search(r"/\S+", primary.representative_message)
        if endpoint_match:
            items.append(f"Endpoint '{endpoint_match.group(0)}' appears in the primary error cluster")

    # Secondary effects — narrative phrasing, no trigger repetition
    for sec in secondary[:3]:
        count = sec.count
        svc = _services_str(sec)
        msg = _trunc(sec.representative_message, 60)

        # Detect queue-growth messages and reformat
        queue_match = re.search(r"(\d+)\s+events?\s+pending", sec.representative_message or "")
        if queue_match:
            depth = queue_match.group(1)
            items.append(
                f"Webhook queue grew to {depth} pending items "
                f"(observed in {count} {'log event' if count == 1 else 'log events'})"
            )
            continue

        if sec.first_seen and primary.first_seen and sec.first_seen >= primary.first_seen:
            if "500" in msg or "error" in msg.lower():
                items.append(f"{count} checkout 500s in {svc} started after the primary failure spike")
            elif "latency" in msg.lower():
                items.append(f"{count} elevated-latency checkout responses followed the same period")
            else:
                items.append(f"{count} '{msg}' events in {svc} (started after primary)")
        else:
            items.append(f"Related: {count} '{msg}' events in {svc}")

    return items[:max_items]


def _services_str(cluster: ClusterData) -> str:
    services = list(cluster.services.keys())
    if not services:
        return "unknown service"
    if len(services) == 1:
        return services[0]
    return ", ".join(services[:3]) + ("..." if len(services) > 3 else "")
