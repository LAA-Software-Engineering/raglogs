"""Metric-type-aware anomaly normalization (#79, Gen-3).

The fresh-corpus external validation (docs/eval-otel-demo.md) showed the abstention
gate's metric arm saturating to 1.0 on *healthy* OTel windows: raw OTLP cumulative
counters were compared by windowed mean, which grows with time, so a healthy window
"looked different" purely because the clock advanced. The fix is metric **semantics**
(not the threshold): interpret each metric by its instrument type before computing an
anomaly.

Per ``(service, metric)`` over a baseline and an incident window:

- **counter / histogram** (cumulative, monotonic): the level is meaningless; the
  *rate* is the signal. Compare incident vs baseline **rate** (total increment over
  the window / window seconds). A steadily-increasing healthy counter has equal rates
  → ~0 anomaly; an incident that accelerates the counter → high.
- **gauge / sum / unknown** (a level): compare the mean **level**, relative change
  ``|mean_inc − mean_base| / (|mean_base| + eps)`` — unchanged from the original gauge
  behavior, so gauge-only sources (e.g. RCAEval's melted columns, ``metric_type`` null)
  are scored exactly as before.

Per service = max over its metrics. Pure (duck-typed rows with ``service`` /
``metric`` / ``value`` / ``ts`` / ``metric_type``); the DB layer and the gate consume
it. Only the *transform* changes — the frozen abstention threshold is untouched.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

_EPS = 1e-9
# metric_type values whose raw value is a cumulative/monotonic quantity → rate.
_CUMULATIVE = frozenset({"counter", "histogram"})


def _seconds(a: datetime, b: datetime) -> float:
    return max((b - a).total_seconds(), 1.0)


def _rate(values: list[float], secs: float) -> float:
    """Increment-per-second of a cumulative series over a window. ``max − min``
    is the total increment for a monotonic counter within the window (robust to the
    exact first/last sample landing); needs ≥2 points to define a rate."""
    if len(values) < 2:
        return 0.0
    return (max(values) - min(values)) / secs


def metric_anomaly_by_type(
    samples, baseline_start: datetime, incident_start: datetime, incident_end: datetime
) -> dict[str, float]:
    """Per-service metric anomaly magnitude, normalized by instrument type.

    ``samples`` are duck-typed rows with ``service`` / ``metric`` / ``value`` / ``ts``
    / ``metric_type``. Returns ``{service: anomaly}`` (max over the service's metrics),
    a dimensionless relative quantity comparable across metric types."""
    base: dict[tuple, list[float]] = defaultdict(list)
    inc: dict[tuple, list[float]] = defaultdict(list)
    mtypes: dict[tuple, str | None] = {}
    for m in samples:
        s, name, v, ts = m.service, m.metric, m.value, m.ts
        if not s or v is None or ts is None:
            continue
        key = (s, name)
        mtypes[key] = getattr(m, "metric_type", None)
        if baseline_start <= ts < incident_start:
            base[key].append(float(v))
        elif incident_start <= ts <= incident_end:
            inc[key].append(float(v))

    pre = _seconds(baseline_start, incident_start)
    post = _seconds(incident_start, incident_end)
    out: dict[str, float] = defaultdict(float)
    for key in set(base) & set(inc):
        service = key[0]
        b, i = base[key], inc[key]
        if (mtypes.get(key) or "").lower() in _CUMULATIVE:
            rb, ri = _rate(b, pre), _rate(i, post)
            if len(b) < 2 or len(i) < 2:
                continue  # can't define a rate from a single sample in a window
            anomaly = abs(ri - rb) / (rb + _EPS)
        else:  # gauge / sum / unknown → level comparison (original behavior)
            import statistics
            bm = statistics.mean(b)
            anomaly = abs(statistics.mean(i) - bm) / (abs(bm) + _EPS)
        out[service] = max(out[service], anomaly)
    return dict(out)
