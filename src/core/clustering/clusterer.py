import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from src.core.clustering.baseline import compute_change_ratio, get_baseline_counts
from src.core.clustering.scoring import compute_importance_score
from src.core.embeddings.provider import EmbeddingsProvider
from src.core.normalization.patterns import is_trigger_message
from src.db.models import (
    DEFAULT_LOG_SCOPE,
    Cluster,
    ClusterMember,
    ClusterRun,
    LogEntry,
)
from src.db.scope_filter import filter_log_entries_by_scope
from src.utils.time import resolve_baseline_window

# Levels that count as a "problem" line for (service, fingerprint) root-cause
# attribution — matches the trivial baseline's error/fatal/critical filter (#82).
_ERROR_LEVELS = ("error", "fatal", "critical")


@dataclass
class ClusterData:
    fingerprint: str
    representative_message: str
    count: int
    services: dict[str, int]
    levels: dict[str, int]
    first_seen: Optional[datetime]
    last_seen: Optional[datetime]
    baseline_count: int
    change_ratio: float
    importance_score: float
    is_trigger: bool = False
    log_entry_ids: list[uuid.UUID] = field(default_factory=list)
    merged_fingerprints: list[str] = field(default_factory=list)
    # Per-service count of error/fatal/critical lines in this cluster. Lets
    # primary selection work at (service, fingerprint) granularity — matching the
    # trivial "most frequent error cluster" selector (#82).
    error_service_counts: dict[str, int] = field(default_factory=dict)


def _build_cluster_data(
    fingerprint: str, group: dict, baseline_counts: dict[str, int]
) -> ClusterData:
    """Build a ClusterData from one fingerprint's grouped log rows."""
    count = len(group["ids"])
    services = dict(group["services"])
    levels = dict(group["levels"])
    error_service_counts = dict(group.get("error_services", {}))
    timestamps = sorted([t for t in group["timestamps"] if t is not None])

    baseline_count = baseline_counts.get(fingerprint, 0)
    change_ratio = compute_change_ratio(count, baseline_count)

    # Representative message: most common
    rep_msg = ""
    if group["messages"]:
        from collections import Counter

        rep_msg = Counter(group["messages"]).most_common(1)[0][0]

    is_trigger = is_trigger_message(rep_msg)

    importance = compute_importance_score(
        count=count,
        levels_distribution=levels,
        change_ratio=change_ratio,
        services_count=len(services),
        is_trigger_correlated=is_trigger,
    )

    return ClusterData(
        fingerprint=fingerprint,
        representative_message=rep_msg,
        count=count,
        services=services,
        levels=levels,
        first_seen=timestamps[0] if timestamps else None,
        last_seen=timestamps[-1] if timestamps else None,
        baseline_count=baseline_count,
        change_ratio=change_ratio,
        importance_score=importance,
        is_trigger=is_trigger,
        log_entry_ids=group["ids"],
        error_service_counts=error_service_counts,
    )


def run_clustering(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    service: Optional[str] = None,
    environment: Optional[str] = None,
    baseline_window_str: str = "24h",
    max_clusters: int = 50,
    save_to_db: bool = True,
    ingestion_job_id: Optional[uuid.UUID] = None,
    scope: str = DEFAULT_LOG_SCOPE,
) -> tuple["ClusterRun", list[ClusterData]]:
    """
    Main clustering pipeline for a time window.
    Returns (ClusterRun, list[ClusterData]) sorted by importance descending.
    """
    from src.observability.metrics import record_cluster_count
    from src.observability.tracing import start_span

    with start_span("cluster", **{"raglogs.scope": scope}):
        cluster_run, top_clusters = _run_clustering(
            db=db,
            window_start=window_start,
            window_end=window_end,
            service=service,
            environment=environment,
            baseline_window_str=baseline_window_str,
            max_clusters=max_clusters,
            save_to_db=save_to_db,
            ingestion_job_id=ingestion_job_id,
            scope=scope,
        )
        record_cluster_count(len(top_clusters))
        return cluster_run, top_clusters


def _run_clustering(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    service: Optional[str] = None,
    environment: Optional[str] = None,
    baseline_window_str: str = "24h",
    max_clusters: int = 50,
    save_to_db: bool = True,
    ingestion_job_id: Optional[uuid.UUID] = None,
    scope: str = DEFAULT_LOG_SCOPE,
) -> tuple["ClusterRun", list[ClusterData]]:
    """
    Main clustering pipeline for a time window.
    Returns (ClusterRun, list[ClusterData]) sorted by importance descending.
    """
    # 1. Query log entries in window
    q = select(
        LogEntry.fingerprint,
        LogEntry.normalized_message,
        LogEntry.service,
        LogEntry.level,
        LogEntry.timestamp,
        LogEntry.id,
    ).where(
        LogEntry.timestamp >= window_start,
        LogEntry.timestamp <= window_end,
        LogEntry.fingerprint.isnot(None),
    )
    q = filter_log_entries_by_scope(q, scope)

    if service:
        q = q.where(LogEntry.service == service)
    if environment:
        q = q.where(LogEntry.environment == environment)
    if ingestion_job_id:
        q = q.where(LogEntry.ingestion_job_id == ingestion_job_id)

    rows = db.execute(q).all()

    if not rows:
        cluster_run = _create_cluster_run(
            db,
            window_start,
            window_end,
            service,
            environment,
            save=save_to_db,
            scope=scope,
        )
        return cluster_run, []

    # 2. Group by fingerprint
    groups: dict[str, dict] = defaultdict(
        lambda: {
            "messages": [],
            "services": defaultdict(int),
            "levels": defaultdict(int),
            "error_services": defaultdict(int),
            "timestamps": [],
            "ids": [],
        }
    )

    for row in rows:
        fp = row.fingerprint
        g = groups[fp]
        if row.normalized_message:
            g["messages"].append(row.normalized_message)
        if row.service:
            g["services"][row.service] += 1
            if row.level and row.level.lower() in _ERROR_LEVELS:
                g["error_services"][row.service] += 1
        if row.level:
            g["levels"][row.level] += 1
        if row.timestamp:
            g["timestamps"].append(row.timestamp)
        g["ids"].append(row.id)

    # 3. Get baseline counts
    # When scoped to a specific ingestion job, skip cross-job baseline: other jobs
    # may contain re-ingested data with overlapping timestamps, making baseline
    # counts meaningless. Treat the scoped run as a fresh first-occurrence baseline.
    if ingestion_job_id:
        baseline_counts: dict[str, int] = {}
    else:
        baseline_start, baseline_end = resolve_baseline_window(
            window_start, window_end, baseline_window_str
        )
        baseline_counts = get_baseline_counts(
            db,
            baseline_start,
            baseline_end,
            service=service,
            environment=environment,
            scope=scope,
        )

    # 4. Build cluster data
    clusters: list[ClusterData] = [
        _build_cluster_data(fp, g, baseline_counts) for fp, g in groups.items()
    ]

    # 5. Optional semantic merge, then rank and cap
    top_clusters, algorithm = rank_and_merge_clusters(clusters, max_clusters)

    # 6. Persist
    cluster_run = _create_cluster_run(
        db,
        window_start,
        window_end,
        service,
        environment,
        save=save_to_db,
        algorithm=algorithm,
        scope=scope,
    )

    if save_to_db:
        _persist_clusters(db, cluster_run, top_clusters)

    _maybe_persist_cluster_embeddings(db, top_clusters, scope)

    return cluster_run, top_clusters


def rank_and_merge_clusters(
    clusters: list[ClusterData],
    max_clusters: int,
    *,
    provider: Optional[EmbeddingsProvider] = None,
) -> tuple[list[ClusterData], str]:
    """Semantic-merge (if enabled), re-sort by importance, apply ``max_clusters``.

    Returns ``(clusters, algorithm)`` where algorithm is ``fingerprint`` when
    embeddings were not used and ``fingerprint+semantic`` when they were.

    The ``max_clusters`` cap is an importance ranking for *display*, but primary
    (root-cause) selection happens downstream over whatever survives it. A quiet
    root cause — few error lines relative to the downstream cascade it triggers —
    can rank below the cap and be dropped, so the primary is then chosen from
    symptoms only (measured on RCAEval RE3: root cause fell to a downstream
    caller in every miss, #82). Guarantee the top root-cause candidate — the
    cluster with the largest single-service error group, which is what primary
    selection keys on — survives the cap.
    """
    from src.core.clustering.semantic_merge import maybe_semantic_merge

    merged, used_semantic = maybe_semantic_merge(clusters, provider=provider)
    merged.sort(key=lambda c: c.importance_score, reverse=True)
    algorithm = "fingerprint+semantic" if used_semantic else "fingerprint"

    top = merged[:max_clusters]
    if len(merged) > len(top):
        candidate = max(
            merged,
            key=lambda c: (max(c.error_service_counts.values(), default=0), c.count),
        )
        # Only guarantee an error-level candidate (matches select_primary_cluster's
        # error-first key). A warn-only root cause can still be capped out — an
        # accepted blind spot for the error/fatal faults #82 targets; revisit if a
        # corpus of warn-only root causes appears.
        if max(candidate.error_service_counts.values(), default=0) > 0 and not any(
            c is candidate for c in top
        ):
            top = [*top, candidate]
    return top, algorithm


def _create_cluster_run(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    service: Optional[str],
    environment: Optional[str],
    save: bool = True,
    algorithm: str = "fingerprint",
    scope: str = DEFAULT_LOG_SCOPE,
) -> ClusterRun:
    run = ClusterRun(
        id=uuid.uuid4(),
        window_start=window_start,
        window_end=window_end,
        service_filter=service,
        environment_filter=environment,
        algorithm=algorithm,
        status="completed",
        scope=scope or DEFAULT_LOG_SCOPE,
    )
    if save:
        db.add(run)
        db.flush()
    return run


# Cap on cluster members persisted per cluster (a sample, for performance).
_MAX_CLUSTER_MEMBERS = 100


def _cluster_and_member_rows(
    cluster_run_id: uuid.UUID, clusters: list[ClusterData]
) -> tuple[list[dict], list[dict]]:
    """Build the Cluster and ClusterMember insert rows for a run.

    Pure (no DB): ids are generated here so members can reference their cluster
    without a per-cluster flush, which lets both be bulk-inserted. Members are
    capped at ``_MAX_CLUSTER_MEMBERS`` per cluster.
    """
    cluster_rows: list[dict] = []
    member_rows: list[dict] = []
    for cd in clusters:
        cluster_id = uuid.uuid4()
        cluster_rows.append(
            {
                "id": cluster_id,
                "cluster_run_id": cluster_run_id,
                "cluster_key": cd.fingerprint,
                "representative_message": (
                    cd.representative_message[:2048] if cd.representative_message else None
                ),
                "fingerprint": cd.fingerprint,
                "count": cd.count,
                "services_json": list(cd.services.keys()),
                "levels_json": cd.levels,
                "first_seen": cd.first_seen,
                "last_seen": cd.last_seen,
                "baseline_count": cd.baseline_count,
                "change_ratio": cd.change_ratio,
                "importance_score": cd.importance_score,
            }
        )
        for log_id in cd.log_entry_ids[:_MAX_CLUSTER_MEMBERS]:
            member_rows.append(
                {"id": uuid.uuid4(), "cluster_id": cluster_id, "log_entry_id": log_id}
            )
    return cluster_rows, member_rows


def _persist_clusters(
    db: Session, cluster_run: ClusterRun, clusters: list[ClusterData]
) -> None:
    """Bulk-insert clusters and their sampled members.

    Mirrors the ingest path's ``pg_insert`` batching: a single executemany for
    clusters and one for members, instead of ~1,000 individual ORM adds with a
    flush per cluster on every explain call.
    """
    cluster_rows, member_rows = _cluster_and_member_rows(cluster_run.id, clusters)
    if cluster_rows:
        db.execute(insert(Cluster), cluster_rows)
    if member_rows:
        db.execute(insert(ClusterMember), member_rows)
    db.flush()


def _maybe_persist_cluster_embeddings(
    db: Session,
    clusters: list[ClusterData],
    scope: str,
) -> None:
    """Upsert cluster template vectors. Fail-open so clustering never raises."""
    if not clusters:
        return
    try:
        from src.core.embeddings.store import persist_cluster_embeddings

        persist_cluster_embeddings(db, clusters, scope=scope)
    except Exception:
        import structlog

        structlog.get_logger().warning(
            "cluster_embeddings_hook_failed",
            count=len(clusters),
            exc_info=True,
        )
