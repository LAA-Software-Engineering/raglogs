import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Union

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.clustering.baseline import compute_change_ratio, get_baseline_counts
from src.core.clustering.scoring import compute_importance_score
from src.core.normalization.patterns import is_trigger_message
from src.db.models import Cluster, ClusterMember, ClusterRun, LogEntry
from src.utils.time import resolve_baseline_window


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

    if service:
        q = q.where(LogEntry.service == service)
    if environment:
        q = q.where(LogEntry.environment == environment)
    if ingestion_job_id:
        q = q.where(LogEntry.ingestion_job_id == ingestion_job_id)

    rows = db.execute(q).all()

    if not rows:
        cluster_run = _create_cluster_run(db, window_start, window_end, service, environment, save=save_to_db)
        return cluster_run, []

    # 2. Group by fingerprint
    groups: dict[str, dict] = defaultdict(lambda: {
        "messages": [],
        "services": defaultdict(int),
        "levels": defaultdict(int),
        "timestamps": [],
        "ids": [],
    })

    for row in rows:
        fp = row.fingerprint
        g = groups[fp]
        if row.normalized_message:
            g["messages"].append(row.normalized_message)
        if row.service:
            g["services"][row.service] += 1
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
        baseline_start, baseline_end = resolve_baseline_window(window_start, window_end, baseline_window_str)
        baseline_counts = get_baseline_counts(db, baseline_start, baseline_end, service=service, environment=environment)

    # 4. Build cluster data
    clusters: list[ClusterData] = []

    for fp, g in groups.items():
        count = len(g["ids"])
        services = dict(g["services"])
        levels = dict(g["levels"])
        timestamps = sorted([t for t in g["timestamps"] if t is not None])

        baseline_count = baseline_counts.get(fp, 0)
        change_ratio = compute_change_ratio(count, baseline_count)

        # Representative message: most common
        rep_msg = ""
        if g["messages"]:
            from collections import Counter
            rep_msg = Counter(g["messages"]).most_common(1)[0][0]

        is_trigger = is_trigger_message(rep_msg)

        importance = compute_importance_score(
            count=count,
            levels_distribution=levels,
            change_ratio=change_ratio,
            services_count=len(services),
            is_trigger_correlated=False,  # refined below after sorting
        )

        clusters.append(ClusterData(
            fingerprint=fp,
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
            log_entry_ids=g["ids"],
        ))

    # 5. Sort by importance
    clusters.sort(key=lambda c: c.importance_score, reverse=True)

    # Limit
    top_clusters = clusters[:max_clusters]

    # 6. Persist
    cluster_run = _create_cluster_run(db, window_start, window_end, service, environment, save=save_to_db)

    if save_to_db:
        _persist_clusters(db, cluster_run, top_clusters)

    return cluster_run, top_clusters


def _create_cluster_run(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    service: Optional[str],
    environment: Optional[str],
    save: bool = True,
) -> ClusterRun:
    run = ClusterRun(
        id=uuid.uuid4(),
        window_start=window_start,
        window_end=window_end,
        service_filter=service,
        environment_filter=environment,
        algorithm="fingerprint",
        status="completed",
    )
    if save:
        db.add(run)
        db.flush()
    return run


def _persist_clusters(db: Session, cluster_run: ClusterRun, clusters: list[ClusterData]) -> None:
    for cd in clusters:
        cluster = Cluster(
            id=uuid.uuid4(),
            cluster_run_id=cluster_run.id,
            cluster_key=cd.fingerprint,
            representative_message=cd.representative_message[:2048] if cd.representative_message else None,
            fingerprint=cd.fingerprint,
            count=cd.count,
            services_json=list(cd.services.keys()),
            levels_json=cd.levels,
            first_seen=cd.first_seen,
            last_seen=cd.last_seen,
            baseline_count=cd.baseline_count,
            change_ratio=cd.change_ratio,
            importance_score=cd.importance_score,
        )
        db.add(cluster)
        db.flush()

        # Add a sample of cluster members (up to 100 for performance)
        sample_ids = cd.log_entry_ids[:100]
        for log_id in sample_ids:
            member = ClusterMember(
                id=uuid.uuid4(),
                cluster_id=cluster.id,
                log_entry_id=log_id,
            )
            db.add(member)

    db.flush()
