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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from src.core.normalization.patterns import infer_trigger_type


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

    # Tiebreak on earliest first_seen (tz-safe via epoch seconds; +inf when absent).
    out.sort(key=lambda t: (-t.score, t.first_seen.timestamp() if t.first_seen else math.inf))
    return out[:max_candidates]
