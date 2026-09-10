"""Rare-event trigger candidate extraction (#82 T1).

The current detector is a regex list; it can't see triggers phrased in ways the
list doesn't anticipate, and finds nothing on faults that announce no log line.
The higher-value signal is **rarity**: a fingerprint that appeared 0 times in the
lookback baseline and shows up right before the error onset is a trigger candidate
*by definition* — regardless of wording or language.

This module extracts those candidates from the already-computed clusters (which
carry ``baseline_count`` / ``change_ratio`` / ``first_seen`` from the in-job
baseline, #115). The regex list is **demoted**: :func:`infer_trigger_type` still
labels a candidate's type and can break ties, but it no longer gates detection.

Pure over ``ClusterData``-like records, so it is unit-testable without a database.
Linkage (traces) and the confidence-gate change are applied by the caller (T2);
this layer only *finds and ranks* candidates and marks whether each is rare.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.core.normalization.patterns import infer_trigger_type


def _epoch(dt: Optional[datetime]) -> float:
    """Sort key seconds, comparable across naive/aware inputs (naive := UTC);
    +inf sorts missing timestamps last."""
    if dt is None:
        return math.inf
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass
class RareTriggerCandidate:
    service: Optional[str]
    fingerprint: Optional[str]
    message: str
    first_seen: Optional[datetime]
    trigger_type: str
    baseline_count: int
    change_ratio: float
    score: float  # rarity x onset-earliness; higher = stronger candidate


def _dominant_service(cluster) -> Optional[str]:
    services = getattr(cluster, "services", None) or {}
    if not services:
        return None
    return max(services.items(), key=lambda kv: kv[1])[0]


def rare_event_candidates(
    clusters,
    onset: Optional[datetime],
    *,
    rare_change_ratio: float = 5.0,
    onset_grace_seconds: int = 120,
    max_candidates: int = 5,
) -> list[RareTriggerCandidate]:
    """Rank rare fingerprints that precede the error ``onset`` as trigger
    candidates.

    A cluster qualifies when it is **rare** — ``baseline_count == 0`` or
    ``change_ratio >= rare_change_ratio`` — and first appears at or before
    ``onset`` (within ``onset_grace_seconds``, so a candidate essentially
    concurrent with onset still counts). Ranked by rarity x onset-earliness:
    earlier, rarer changes score higher. ``onset=None`` (no primary error cluster)
    ranks purely by rarity.
    """
    cutoff = onset + timedelta(seconds=onset_grace_seconds) if onset else None
    out: list[RareTriggerCandidate] = []
    for c in clusters:
        baseline_count = int(getattr(c, "baseline_count", 0) or 0)
        change_ratio = float(getattr(c, "change_ratio", 0.0) or 0.0)
        is_rare = baseline_count == 0 or change_ratio >= rare_change_ratio
        if not is_rare:
            continue
        first_seen = getattr(c, "first_seen", None)
        if cutoff is not None and first_seen is not None and first_seen > cutoff:
            continue  # appears after onset -> can't have triggered it

        rarity = 1.0 / (baseline_count + 1)  # 1.0 when never seen before
        earliness = 0.0
        if onset is not None and first_seen is not None:
            lead = (onset - first_seen).total_seconds()  # >0 = before onset
            earliness = math.log1p(max(lead, 0.0))
        score = rarity * (1.0 + earliness)

        message = getattr(c, "representative_message", "") or ""
        out.append(RareTriggerCandidate(
            service=_dominant_service(c),
            fingerprint=getattr(c, "fingerprint", None),
            message=message,
            first_seen=first_seen,
            trigger_type=infer_trigger_type(message) or "none",
            baseline_count=baseline_count,
            change_ratio=change_ratio,
            score=score,
        ))

    # Tiebreak on earliest first_seen; _epoch normalizes naive/aware and sorts
    # missing timestamps last.
    out.sort(key=lambda t: (-t.score, _epoch(t.first_seen)))
    return out[:max_candidates]


@dataclass
class AnomalyOnset:
    """A metric's first significant deviation from its pre-incident baseline — a
    trigger candidate for faults that write no log line (RCAEval RE2 resource /
    network faults; #82 T3 showed logs-only rare-event finds nothing on these)."""

    service: Optional[str]
    metric: str
    onset: datetime
    magnitude: float  # deviation in baseline sigmas (or relative units when flat)
    score: float


def metric_anomaly_onsets(
    samples,
    incident_start: datetime,
    incident_end: datetime,
    *,
    min_baseline: int = 3,
    z_threshold: float = 3.0,
    rel_threshold: float = 0.5,
    max_candidates: int = 5,
) -> list[AnomalyOnset]:
    """Earliest per-(service, metric) deviation from baseline, as onset candidates.

    ``samples`` are duck-typed rows with ``service`` / ``metric`` / ``value`` /
    ``ts``. Baseline = points with ``ts < incident_start`` (needs ``min_baseline``
    of them); a point in ``[incident_start, incident_end]`` is anomalous when it is
    more than ``z_threshold`` baseline sigmas from the baseline mean, or — when the
    baseline is flat (sigma ~ 0) — more than ``rel_threshold`` of |mean| away. The
    earliest anomalous point per series is its onset; ranked by earliness x
    magnitude. This surfaces the injection time on faults that never log.
    """
    baseline: dict[tuple, list[float]] = {}
    incident: dict[tuple, list[tuple[datetime, float]]] = {}
    for m in samples:
        service = getattr(m, "service", None)
        metric = getattr(m, "metric", None)
        ts = getattr(m, "ts", None)
        value = getattr(m, "value", None)
        if metric is None or ts is None or value is None:
            continue
        key = (service, metric)
        if ts < incident_start:
            baseline.setdefault(key, []).append(float(value))
        elif incident_start <= ts <= incident_end:
            incident.setdefault(key, []).append((ts, float(value)))

    onsets: list[AnomalyOnset] = []
    for key, base in baseline.items():
        pts = incident.get(key)
        if pts is None or len(base) < min_baseline:
            continue
        mean = statistics.mean(base)
        sigma = statistics.pstdev(base)
        for ts, value in sorted(pts, key=lambda p: p[0]):
            dev = abs(value - mean)
            if sigma > 0:
                mag = dev / sigma
                anomalous = mag >= z_threshold
            else:
                denom = abs(mean) if mean else 1.0
                mag = dev / denom
                anomalous = dev > rel_threshold * denom
            if anomalous:
                lead = max((incident_end - ts).total_seconds(), 0.0)
                onsets.append(AnomalyOnset(
                    service=key[0], metric=key[1], onset=ts, magnitude=mag,
                    score=mag * math.log1p(lead),
                ))
                break  # earliest anomalous point per series is the onset

    onsets.sort(key=lambda o: (-o.score, _epoch(o.onset)))
    return onsets[:max_candidates]
