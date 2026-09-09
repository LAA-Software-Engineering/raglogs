import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, not_, or_, select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.core.clustering.clusterer import ClusterData
from src.core.normalization.patterns import TRIGGER_PATTERNS, is_trigger_message
from src.db.models import DEFAULT_LOG_SCOPE, LogEntry
from src.db.scope_filter import filter_log_entries_by_scope

# TRIGGER_PATTERNS translated to Postgres's case-insensitive regex operator so
# trigger matching happens in the WHERE clause instead of after a row cap has
# already discarded everything past the first N log lines in range (#76 review:
# ordering by timestamp before LIMIT makes *which* rows are examined
# deterministic, but on a busy scope the earliest N rows can all be non-trigger
# noise, silently excluding a real trigger that occurs later in range). None of
# the patterns use \b, lookaround, or other constructs where Python's `re` and
# Postgres's ARE dialect diverge — verified match-for-match against a live
# Postgres for every pattern in tests/integration/test_trigger_search.py.
_TRIGGER_SQL_PATTERNS = [p.pattern for p in TRIGGER_PATTERNS]

# Safety valve, not the primary recall mechanism now that the WHERE clause
# already narrows to trigger-shaped rows: real trigger phrasing is rare, so
# this should only ever bind on pathological/adversarial log content.
_TRIGGER_ROW_CAP = 5000


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


def _primary_service(primary: Optional["ClusterData"]) -> Optional[str]:
    """The erroring service a trigger must link to — the primary cluster's
    top error-service, else its highest-volume service."""
    if primary is None:
        return None
    esc = getattr(primary, "error_service_counts", None) or {}
    pool = esc or (primary.services or {})
    return max(pool.items(), key=lambda kv: kv[1])[0] if pool else None


def _rare_event_triggers(
    db: Session,
    clusters: list["ClusterData"],
    primary: Optional["ClusterData"],
    window_start: datetime,
    window_end: datetime,
    scope: str,
) -> tuple[list[TriggerCandidate], bool, bool]:
    """Rare-event trigger detection (#82): rank rare fingerprints near onset, then
    gate on trace/service linkage to the erroring service.

    Returns ``(candidates, trigger_found, trigger_explains)``. ``trigger_found`` =
    a rare change occurred near onset; ``trigger_explains`` = at least one such
    change is *linked* to the erroring service (same service, or connected in the
    trace-derived dependency graph). Queue/async boundaries that leave no span
    edge simply don't link — a *skip, not fail* (found stays true, explains false),
    which honestly reports "a change happened but nothing ties it to these errors".
    """
    from src.core.rca.linkage import build_service_graph
    from src.core.rca.triggers import rare_event_candidates

    onset = primary.first_seen if primary else None
    # A cluster can't be its own trigger — exclude the primary error cluster from
    # the candidate pool (else its own rarity/onset would "explain" itself).
    pool = [c for c in clusters if c is not primary]
    rare = rare_event_candidates(
        pool, onset, rare_change_ratio=get_settings().trigger_rare_change_ratio
    )
    candidates = [
        TriggerCandidate(message=r.message, timestamp=r.first_seen or onset or window_start, service=r.service)
        for r in rare
    ]
    if not rare:
        return candidates, False, False

    root_service = _primary_service(primary)
    graph = build_service_graph(db, scope, window_start, window_end)
    explains = any(
        r.service and root_service and graph.linked(r.service, root_service) for r in rare
    )
    return candidates, True, explains


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
    # #82: a rare change occurred near onset (trigger_found) vs. that change is
    # rare AND linked to the erroring service (trigger_explains — the validated
    # signal that may gate "high" confidence). None = not evaluated; confidence
    # then falls back to bool(trigger_candidates) for exact back-compat.
    trigger_found: Optional[bool] = None
    trigger_explains: Optional[bool] = None


def find_trigger_candidates(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    lookback_minutes: Optional[int] = None,
    ingestion_job_id: Optional[uuid.UUID] = None,
    scope: str = DEFAULT_LOG_SCOPE,
) -> list[TriggerCandidate]:
    """
    Find likely trigger events in a window slightly before the main window.

    The WHERE clause filters to rows the Python extraction below would
    actually evaluate a trigger match against, before ordering/capping, so the
    row cap bounds trigger-shaped candidates rather than arbitrary log volume:
      - normalized_message, when it's populated (the common case) — this is
        the only text the Python fallback reads in that case.
      - raw_message, only when normalized_message is empty/null — matching
        the Python fallback exactly, which never reads raw_message otherwise.
    Matching raw_message unconditionally would let a row with a benign
    normalized_message but a raw JSON blob that incidentally contains
    trigger-shaped text in some other field (an error/detail field, a stack
    trace) consume a cap slot as a false positive: is_trigger_message() would
    correctly reject it since it never sees raw_message in that case, but by
    then the cap has already spent the slot, reopening the same starvation
    this filter exists to close — just triggered by raw-JSON noise instead of
    log volume (#76 review round 2).

    Without any of this, ORDER BY timestamp LIMIT N alone is deterministic but
    still silently drops a real trigger whenever more than N unrelated log
    lines occur earlier in the search range than it does (#76 review round 1).
    is_trigger_message() re-checks every SQL match in Python as a final
    arbiter — cheap here since the SQL filter has already narrowed the result
    set to a small candidate set, and it stays authoritative for the rare
    raw-JSON-fallback rows where the SQL side can still be an imprecise
    superset (e.g. a trigger phrase in a non-"message" JSON field on a line
    whose "message" field happens to be missing or unparseable).

    lookback_minutes defaults to settings.trigger_lookback_minutes when not
    given explicitly, so TRIGGER_LOOKBACK_MINUTES has one source of truth
    rather than a second hardcoded default here that callers could silently
    diverge from.
    """
    from datetime import timedelta

    if lookback_minutes is None:
        lookback_minutes = get_settings().trigger_lookback_minutes

    search_start = window_start - timedelta(minutes=lookback_minutes)

    normalized_populated = and_(
        LogEntry.normalized_message.isnot(None),
        LogEntry.normalized_message != "",
    )
    trigger_match = or_(
        and_(
            normalized_populated,
            or_(*(LogEntry.normalized_message.op("~*")(p) for p in _TRIGGER_SQL_PATTERNS)),
        ),
        and_(
            not_(normalized_populated),
            or_(*(LogEntry.raw_message.op("~*")(p) for p in _TRIGGER_SQL_PATTERNS)),
        ),
    )

    q = select(
        LogEntry.normalized_message,
        LogEntry.raw_message,
        LogEntry.timestamp,
        LogEntry.service,
    ).where(
        LogEntry.timestamp >= search_start,
        LogEntry.timestamp <= window_end,
        trigger_match,
    )
    q = filter_log_entries_by_scope(q, scope)
    if ingestion_job_id:
        q = q.where(LogEntry.ingestion_job_id == ingestion_job_id)
    q = q.order_by(LogEntry.timestamp, LogEntry.id).limit(_TRIGGER_ROW_CAP)

    rows = db.execute(q).all()

    candidates = []
    for row in rows:
        # Prefer normalized_message; fall back to extracting message from raw JSON
        msg = row.normalized_message or ""
        if not msg and row.raw_message:
            try:
                import orjson
                parsed = orjson.loads(row.raw_message)
                msg = parsed.get("message") or row.raw_message
            except Exception:
                msg = row.raw_message
        if is_trigger_message(msg):
            candidates.append(TriggerCandidate(
                message=msg[:500],
                timestamp=row.timestamp,
                service=row.service,
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


def _top_error_service_count(c: ClusterData) -> int:
    """Line count of this cluster's largest single (service, error-level) group."""
    return max(c.error_service_counts.values(), default=0)


def _onset_earliness(
    first_seen: Optional[datetime],
    window_start: Optional[datetime],
    window_end: Optional[datetime],
) -> float:
    """Where a cluster's first log falls in the window: 1.0 at the start (a cause
    precedes its cascade), 0.0 at the end. Neutral 0.5 without timing."""
    if first_seen is None or window_start is None or window_end is None:
        return 0.5
    span = (window_end - window_start).total_seconds()
    if span <= 0:
        return 0.5
    frac = (window_end - first_seen).total_seconds() / span
    return 0.0 if frac < 0.0 else 1.0 if frac > 1.0 else frac


def _candidate_score(
    c: ClusterData,
    window_start: Optional[datetime],
    window_end: Optional[datetime],
) -> float:
    """Anomaly/onset-aware root-cause score for a cluster's top (service,
    fingerprint) error group (#82): weighted log(volume) + log(change_ratio+1)
    [anomaly vs the in-job baseline] + onset earliness. Weights are configurable;
    zeroing anomaly+onset recovers the pure most-frequent-error baseline."""
    s = get_settings()
    volume = math.log(_top_error_service_count(c) + 1)
    anomaly = math.log(c.change_ratio + 1.0)
    onset = _onset_earliness(c.first_seen, window_start, window_end)
    return (
        s.rca_weight_volume * volume
        + s.rca_weight_anomaly * anomaly
        + s.rca_weight_onset * onset
    )


def select_primary_cluster(
    significant_clusters: list[ClusterData],
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> Optional[ClusterData]:
    """Pick the primary (root-cause) cluster.

    Ranks candidates by a configurable blend of error volume, anomaly against the
    in-job baseline (``change_ratio``), and onset earliness at (service,
    fingerprint) granularity. **By default only volume is weighted** — that is
    the trivial "most frequent error/fatal cluster" selector, which reaches
    parity with the baseline. Adding anomaly+onset was measured on RCAEval and
    did not robustly lift across corpora (RE3 +1 case, RE2 -1 case; #82), so the
    weights default to 0 and the blend is kept for a corpus where it generalizes.
    Fall back to the highest-volume cluster when nothing is error-level so an
    explanation is still produced.
    """
    if not significant_clusters:
        return None
    error_clusters = [c for c in significant_clusters if _top_error_service_count(c) > 0]
    if error_clusters:
        return max(
            error_clusters,
            key=lambda c: (
                _candidate_score(c, window_start, window_end),
                c.count,
                c.importance_score,
            ),
        )
    return max(significant_clusters, key=lambda c: (c.count, c.importance_score))


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
        if any(lvl in ("error", "fatal", "warn", "critical") for lvl in c.levels)
    ]

    if not significant_clusters:
        significant_clusters = clusters

    primary = select_primary_cluster(significant_clusters, window_start, window_end)
    # Sort secondary by count descending — surface highest-volume effects first.
    # Take from all remaining significant clusters (all but the chosen primary).
    secondary = sorted(
        (c for c in significant_clusters if c is not primary),
        key=lambda c: c.count,
        reverse=True,
    )[:4]

    # Trigger candidates. Default "regex" mode uses the legacy TRIGGER_PATTERNS
    # search; "rare_event" mode (#82) derives them from rare fingerprints near
    # onset + trace/service linkage. trigger_found/trigger_explains stay None in
    # regex mode so confidence is byte-identical (falls back to the old
    # bool(trigger_candidates)).
    trigger_found: Optional[bool] = None
    trigger_explains: Optional[bool] = None
    if get_settings().trigger_mode == "rare_event":
        triggers, trigger_found, trigger_explains = _rare_event_triggers(
            db, clusters, primary, window_start, window_end, scope
        )
    else:
        triggers = find_trigger_candidates(
            db,
            window_start,
            window_end,
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
        trigger_found=trigger_found,
        trigger_explains=trigger_explains,
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

        # Detect queue/backlog-growth messages and reformat generically
        queue_match = re.search(r"(\d+)\s+events?\s+pending", sec.representative_message or "")
        if queue_match:
            depth = queue_match.group(1)
            items.append(
                f"Queue depth grew to {depth} pending items in {svc} "
                f"(observed in {count} {'log event' if count == 1 else 'log events'})"
            )
            continue

        if sec.first_seen and primary.first_seen and sec.first_seen >= primary.first_seen:
            # Generic HTTP-status / latency signals — no domain vocabulary.
            if re.search(r"\b5\d\d\b", msg) or "error" in msg.lower():
                items.append(f"{count} 5xx/error responses in {svc} started after the primary failure spike")
            elif "latency" in msg.lower():
                items.append(f"{count} elevated-latency responses in {svc} followed the same period")
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
