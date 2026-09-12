"""Metric-type-aware, hierarchical metric anomaly for the abstention gate (#79).

Two failures surfaced by the external validation, fixed in layers:

1. **Metric semantics.** Raw OTLP cumulative counters compared by windowed *mean*
   grow with time, so a healthy window "looks different" just because the clock
   advanced. Fix: interpret each metric by instrument type — counters/histograms
   by **rate**, gauges/sums by **level**.
2. **Multiple comparisons + near-zero baselines.** With ~27k metrics, ``max`` over a
   relative change ``|inc − base| / (base + eps)`` always finds one metric that
   twitched (and a near-zero baseline makes it explode). Fix, per the Gen-3.1
   design: a **stable, bounded** per-metric anomaly (log-space effect size, no
   divide-by-tiny-baseline) plus **hierarchical aggregation with corroboration** — a
   service is anomalous because *several* of its metrics agree, not because one of
   thousands spiked.

Pipeline::

    metric → stable effect size |log1p(q_inc) − log1p(q_base)| → saturate → a_m ∈ [0,1]
           → per service: require ≥k metrics with a_m ≥ T, else 0; else mean(top-k)
           → service anomaly → max across services → metric arm

``q`` is the window **rate** for cumulative counters/histograms (increment/sec, with
Prometheus-style reset handling) and the mean **level** for gauges/sums/untyped.
Pure (duck-typed rows: ``service`` / ``metric`` / ``value`` / ``ts`` / ``metric_type``);
all parameters (``tau`` / ``k`` / ``corroboration_threshold``) are selected on
development data and frozen — see ``docs/eval-abstention.md``.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime

from src.core.rca.abstention import saturate  # single source of truth for the transform

_CUMULATIVE = frozenset({"counter", "histogram"})


def _seconds(a: datetime, b: datetime) -> float:
    return max((b - a).total_seconds(), 1.0)


def _rate(points: list[tuple[datetime, float]], secs: float) -> float:
    """Increment-per-second of a cumulative series over a window. Sums the positive
    deltas between time-sorted consecutive samples so a counter **reset** (a drop on
    pod restart / redeploy) contributes 0 rather than a negative/spurious increment
    (Prometheus-style). Needs ≥2 points to define a rate."""
    if len(points) < 2:
        return 0.0
    vals = [v for _, v in sorted(points)]
    return sum(max(0.0, b - a) for a, b in zip(vals, vals[1:])) / secs


def _per_metric_anomaly(
    key_base: list[tuple[datetime, float]],
    key_inc: list[tuple[datetime, float]],
    is_cumulative: bool,
    pre: float,
    post: float,
    tau: float,
) -> float | None:
    """Stable, bounded anomaly for one metric. Log-space effect size
    ``|log1p(q_inc) − log1p(q_base)|`` (symmetric, no divide-by-tiny-baseline), then
    saturate. ``None`` when there isn't enough support to compute the quantity."""
    if is_cumulative:
        if len(key_base) < 2 or len(key_inc) < 2:  # need ≥2 points for a rate
            return None
        q_base, q_inc = _rate(key_base, pre), _rate(key_inc, post)
    else:  # level
        q_base = statistics.mean([v for _, v in key_base])
        q_inc = statistics.mean([v for _, v in key_inc])
    effect = abs(math.log1p(max(0.0, q_inc)) - math.log1p(max(0.0, q_base)))
    return saturate(effect, tau)


def metric_anomaly_by_type(
    samples,
    baseline_start: datetime,
    incident_start: datetime,
    incident_end: datetime,
    *,
    tau: float,
    k: int = 3,
    corroboration_threshold: float = 0.5,
) -> dict[str, float]:
    """Per-service metric anomaly in ``[0, 1]``, hierarchical + corroborated.

    Per ``(service, metric)`` → a stable bounded anomaly ``a_m``. Per service: if
    fewer than ``k`` metrics reach ``corroboration_threshold`` the service scores 0
    (a lone twitch among thousands is not evidence); otherwise the service scores the
    **mean of its top-k** anomalies (robust to a single spike). Returns
    ``{service: score}`` for services that clear corroboration."""
    base: dict[tuple, list[tuple[datetime, float]]] = defaultdict(list)
    inc: dict[tuple, list[tuple[datetime, float]]] = defaultdict(list)
    mtypes: dict[tuple, str | None] = {}
    for m in samples:
        s, name, v, ts = m.service, m.metric, m.value, m.ts
        if not s or v is None or ts is None:
            continue
        key = (s, name)
        mtypes[key] = getattr(m, "metric_type", None)
        if baseline_start <= ts < incident_start:
            base[key].append((ts, float(v)))
        elif incident_start <= ts <= incident_end:
            inc[key].append((ts, float(v)))

    pre = _seconds(baseline_start, incident_start)
    post = _seconds(incident_start, incident_end)
    per_service: dict[str, list[float]] = defaultdict(list)
    for key in set(base) & set(inc):
        is_cum = (mtypes.get(key) or "").lower() in _CUMULATIVE
        a = _per_metric_anomaly(base[key], inc[key], is_cum, pre, post, tau)
        if a is not None:
            per_service[key[0]].append(a)

    out: dict[str, float] = {}
    for service, anoms in per_service.items():
        if sum(1 for a in anoms if a >= corroboration_threshold) < k:
            continue  # not enough corroborating metrics — a lone twitch, not a fault
        top = sorted(anoms, reverse=True)[:k]
        out[service] = statistics.mean(top)
    return out
