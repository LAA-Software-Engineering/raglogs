"""
Compare two time windows by their cluster sets.

Produces a CompareResult with:
  - new_clusters      present in window A, absent in baseline B
  - disappeared       present in B, absent in A
  - increased         in both, count grew significantly
  - decreased         in both, count shrank significantly
  - stable            in both, roughly same volume
  - new_triggers      trigger candidates in A not seen in B

Noise reduction: before diffing, clusters are collapsed by semantic group.
Individual webhook retry events (evt_XXXXXX) are merged into one entry.
Queue-growth events with different depths are merged into one entry.
This mirrors the deduplication done in the timeline builder.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import re

from src.core.clustering.clusterer import ClusterData


# A cluster is "significantly changed" if count moved by more than this factor
CHANGE_THRESHOLD = 1.5


# ── Noise deduplication ───────────────────────────────────────────────────────

def _is_retry(message: str) -> bool:
    return bool(
        re.search(r"retry", message, re.IGNORECASE)
        and re.search(r"evt_", message, re.IGNORECASE)
    )

def _is_queue_growth(message: str) -> bool:
    return bool(re.search(r"queue.{0,30}(growing|pending|backlog)", message, re.IGNORECASE)
                or re.search(r"(pending|backlog).{0,30}queue", message, re.IGNORECASE))


def _collapse_clusters(clusters: list[ClusterData]) -> list[ClusterData]:
    """
    Merge noisy per-event clusters into single representative entries:
      - All retry evt_XXXXXX clusters → one "Webhook retries" cluster
      - All queue-growth clusters     → one "Webhook queue growing" cluster

    All other clusters pass through unchanged.
    """
    normal: list[ClusterData] = []
    retry_group: list[ClusterData] = []
    queue_group: list[ClusterData] = []

    for c in clusters:
        msg = c.representative_message or ""
        if _is_retry(msg):
            retry_group.append(c)
        elif _is_queue_growth(msg):
            queue_group.append(c)
        else:
            normal.append(c)

    def _merge(group: list[ClusterData], label: str, key: str = "") -> ClusterData:
        total = sum(c.count for c in group)
        services: dict[str, int] = {}
        for c in group:
            for svc, cnt in c.services.items():
                services[svc] = services.get(svc, 0) + cnt
        first = min((c.first_seen for c in group if c.first_seen), default=None)
        last  = max((c.last_seen  for c in group if c.last_seen),  default=None)
        return ClusterData(
            fingerprint=f"__collapsed_{key or label}__",
            representative_message=label,
            count=total,
            services=services,
            levels={},
            first_seen=first,
            last_seen=last,
            baseline_count=0,
            change_ratio=float(total),
            importance_score=float(total),
        )

    if retry_group:
        n = len(retry_group)
        total = sum(c.count for c in retry_group)
        normal.append(_merge(retry_group, f"Webhook retries ({n} distinct events, {total} total)", key="retries"))
    if queue_group:
        normal.append(_merge(queue_group, "Webhook queue growing", key="queue_growth"))

    return normal


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ClusterDiff:
    fingerprint: str
    message: str
    services: list[str]
    count_a: Optional[int]   # None if absent in A
    count_b: Optional[int]   # None if absent in B

    @property
    def direction(self) -> str:
        if self.count_a is None:
            return "disappeared"
        if self.count_b is None:
            return "new"
        ratio = self.count_a / max(self.count_b, 1)
        if ratio >= CHANGE_THRESHOLD:
            return "increased"
        if ratio <= 1 / CHANGE_THRESHOLD:
            return "decreased"
        return "stable"


@dataclass
class TriggerDiff:
    message: str
    service: str
    only_in: str  # "a" or "b"


@dataclass
class CompareResult:
    window_a_start: datetime
    window_a_end: datetime
    window_b_start: datetime
    window_b_end: datetime

    new_clusters: list[ClusterDiff] = field(default_factory=list)
    disappeared_clusters: list[ClusterDiff] = field(default_factory=list)
    increased_clusters: list[ClusterDiff] = field(default_factory=list)
    decreased_clusters: list[ClusterDiff] = field(default_factory=list)
    stable_clusters: list[ClusterDiff] = field(default_factory=list)
    new_triggers: list[TriggerDiff] = field(default_factory=list)
    dropped_triggers: list[TriggerDiff] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.new_clusters
            or self.disappeared_clusters
            or self.increased_clusters
            or self.decreased_clusters
            or self.new_triggers
        )


# ── Core diff logic ───────────────────────────────────────────────────────────

def compare_windows(
    clusters_a: list[ClusterData],
    clusters_b: list[ClusterData],
    triggers_a: list,
    triggers_b: list,
    window_a_start: datetime,
    window_a_end: datetime,
    window_b_start: datetime,
    window_b_end: datetime,
) -> CompareResult:
    """
    Diff two cluster sets by fingerprint.

    clusters_a = current/incident window
    clusters_b = baseline window
    """
    result = CompareResult(
        window_a_start=window_a_start,
        window_a_end=window_a_end,
        window_b_start=window_b_start,
        window_b_end=window_b_end,
    )

    # Collapse noisy per-event clusters before diffing
    collapsed_a = _collapse_clusters(clusters_a)
    collapsed_b = _collapse_clusters(clusters_b)

    map_a = {c.fingerprint: c for c in collapsed_a}
    map_b = {c.fingerprint: c for c in collapsed_b}

    for fp in set(map_a) | set(map_b):
        ca = map_a.get(fp)
        cb = map_b.get(fp)

        msg = (ca or cb).representative_message  # type: ignore[union-attr]
        services = list((ca or cb).services.keys())  # type: ignore[union-attr]

        diff = ClusterDiff(
            fingerprint=fp,
            message=msg,
            services=services,
            count_a=ca.count if ca else None,
            count_b=cb.count if cb else None,
        )

        direction = diff.direction
        if direction == "new":
            result.new_clusters.append(diff)
        elif direction == "disappeared":
            result.disappeared_clusters.append(diff)
        elif direction == "increased":
            result.increased_clusters.append(diff)
        elif direction == "decreased":
            result.decreased_clusters.append(diff)
        else:
            result.stable_clusters.append(diff)

    result.new_clusters.sort(key=lambda d: d.count_a or 0, reverse=True)
    result.disappeared_clusters.sort(key=lambda d: d.count_b or 0, reverse=True)
    result.increased_clusters.sort(key=lambda d: (d.count_a or 0) - (d.count_b or 0), reverse=True)
    result.decreased_clusters.sort(key=lambda d: (d.count_b or 0) - (d.count_a or 0), reverse=True)

    # Trigger diff: normalize by message prefix to handle version strings
    def _trigger_key(t) -> str:
        msg = (t.message or "").lower().strip()
        # Strip trailing version strings so "Deploy ... v2.4.1" and
        # "Deploy ... v2.3.9" normalise to the same key
        msg = re.sub(r"\bv\d+[\d.\-a-z]*$", "", msg).strip()
        return msg

    keys_a = {_trigger_key(t) for t in triggers_a}
    keys_b = {_trigger_key(t) for t in triggers_b}

    for t in triggers_a:
        if _trigger_key(t) not in keys_b:
            result.new_triggers.append(TriggerDiff(
                message=t.message, service=t.service or "", only_in="a"
            ))
    for t in triggers_b:
        if _trigger_key(t) not in keys_a:
            result.dropped_triggers.append(TriggerDiff(
                message=t.message, service=t.service or "", only_in="b"
            ))

    return result
