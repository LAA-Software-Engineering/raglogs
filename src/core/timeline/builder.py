"""
Timeline reconstruction from an EvidencePacket.

Ordering:
  1. Trigger/deploy/startup events by their actual timestamp
  2. Primary error by its first_seen
  3. Effects and symptoms after the primary, sorted by severity then timestamp:
     - 500/error effects before latency/other effects
     - symptoms last

Deduplication: individual webhook retry events (evt_XXXXXX) are collapsed
into a single "N webhook retries" effect to reduce noise.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import re

from src.core.explain.evidence import EvidencePacket, TriggerCandidate
from src.core.clustering.clusterer import ClusterData


# Lower = sorts earlier within the post-error bucket
EFFECT_SEVERITY = {
    "error":   0,
    "effect":  1,
    "symptom": 2,
}


@dataclass
class TimelineEvent:
    timestamp: datetime
    category: str          # deploy | startup | trigger | error | effect | symptom
    label: str
    description: str
    count: Optional[int]
    services: list[str]
    duration_minutes: Optional[int] = None


def _trunc(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    cut = text[:n]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n // 2 else cut) + "…"


def _is_retry_noise(message: str) -> bool:
    """Individual webhook retry lines (evt_XXXXXXX) are noise — collapse them."""
    return bool(re.search(r"retry", message, re.IGNORECASE) and re.search(r"evt_", message, re.IGNORECASE))


def _classify_trigger(message: str) -> str:
    msg = message.lower()
    if any(kw in msg for kw in ("deploy", "release", "rollout", "pushed", "promote")):
        return "deploy"
    if any(kw in msg for kw in ("started", "starting", "boot", "launch", "listening", "port")):
        return "startup"
    return "trigger"


def _effect_severity(cluster: ClusterData) -> int:
    """Lower = more severe = sorts first among effects."""
    msg = (cluster.representative_message or "").lower()
    if any(kw in msg for kw in ("500", "error", "fail", "exception", "refused", "denied")):
        return 0
    if any(kw in msg for kw in ("latency", "slow", "timeout", "degraded")):
        return 1
    return 2


def _classify_secondary(cluster: ClusterData, primary: Optional[ClusterData]) -> str:
    msg = (cluster.representative_message or "").lower()
    if re.search(r"queue|pending|backlog", msg):
        return "symptom"
    if primary and cluster.fingerprint == primary.fingerprint:
        return "error"
    if any(kw in msg for kw in ("500", "error", "fail", "exception", "refused", "denied")):
        return "effect"
    if any(kw in msg for kw in ("latency", "slow", "timeout", "degraded")):
        return "effect"
    return "effect"


def _duration(cluster: ClusterData) -> Optional[int]:
    if cluster.first_seen and cluster.last_seen:
        minutes = int((cluster.last_seen - cluster.first_seen).total_seconds() / 60)
        return minutes if minutes > 0 else None
    return None


def build_timeline(packet: EvidencePacket) -> list[TimelineEvent]:
    pre_events: list[TimelineEvent] = []
    error_event: Optional[TimelineEvent] = None
    post_events: list[TimelineEvent] = []

    # ── Triggers ──────────────────────────────────────────────────────────────
    for t in packet.trigger_candidates:
        if not t.timestamp:
            continue
        cat = _classify_trigger(t.message)
        pre_events.append(TimelineEvent(
            timestamp=t.timestamp,
            category=cat,
            label=cat,
            description=_trunc(t.message, 100),
            count=None,
            services=[t.service] if t.service else [],
        ))
    pre_events.sort(key=lambda e: e.timestamp)

    # ── Primary error ─────────────────────────────────────────────────────────
    pc = packet.primary_cluster
    if pc and pc.first_seen:
        error_event = TimelineEvent(
            timestamp=pc.first_seen,
            category="error",
            label="error ↑",
            description=_trunc(pc.representative_message, 100),
            count=pc.count,
            services=list(pc.services.keys()),
            duration_minutes=_duration(pc),
        )

    # ── Secondary clusters (effects/symptoms) ─────────────────────────────────
    primary_ts = error_event.timestamp if error_event else None
    retry_clusters: list[ClusterData] = []

    for sec in packet.secondary_clusters:
        if not sec.first_seen:
            continue

        # Collect retry noise separately for collapsing
        if _is_retry_noise(sec.representative_message or ""):
            retry_clusters.append(sec)
            continue

        cat = _classify_secondary(sec, pc)
        ts = max(sec.first_seen, primary_ts) if primary_ts else sec.first_seen
        post_events.append(TimelineEvent(
            timestamp=ts,
            category=cat,
            label=cat,
            description=_trunc(sec.representative_message, 100),
            count=sec.count,
            services=list(sec.services.keys()),
            duration_minutes=_duration(sec),
        ))

    # Collapse all retry clusters into one "N webhook retries" effect
    if retry_clusters:
        total_retries = sum(c.count for c in retry_clusters)
        services = sorted(set(s for c in retry_clusters for s in c.services))
        earliest = min(c.first_seen for c in retry_clusters)
        ts = max(earliest, primary_ts) if primary_ts else earliest
        post_events.append(TimelineEvent(
            timestamp=ts,
            category="effect",
            label="effect",
            description=f"Webhook retries ({total_retries} retry events)",
            count=total_retries,
            services=services,
        ))

    # Sort effects by: severity first, then timestamp
    post_events.sort(key=lambda e: (
        e.timestamp,
        EFFECT_SEVERITY.get(e.category, 1),
        _effect_severity_from_desc(e.description),
    ))

    result = pre_events
    if error_event:
        result = result + [error_event]
    result = result + post_events
    return result


def _effect_severity_from_desc(description: str) -> int:
    """Sort key for within-category ordering of effect events."""
    msg = description.lower()
    if any(kw in msg for kw in ("500", "error", "fail")):
        return 0
    if any(kw in msg for kw in ("latency", "slow", "timeout")):
        return 1
    return 2
