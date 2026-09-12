"""Multi-modal RCA feature extraction (#118 C1b).

Reproduces the validated spike's per-service features (60% RE3 leave-one-system-out;
see ``docs/spike-multimodal-rca.md``) but reads them from the pipeline's persisted
telemetry — ``log_entries`` + ``trace_spans`` + ``metric_samples`` — for a
``(scope, window)`` instead of raw parquet. Nothing in the explain/RCA path reads
these yet: this is the feature layer the ranker (C2) will consume.

Per candidate service, over the incident window ``[incident_start, incident_end]``
vs the baseline window ``[baseline_start, incident_start)``:

  ``log_err``    error-level log lines (incident window)
  ``log_grp``    largest ``(service, fingerprint)`` error group (incident window)
  ``log_stack``  originating stack-trace lines (incident window)
  ``tr_rate``    span-rate ratio incident/baseline
  ``tr_dur``     p95-duration ratio incident/baseline
  ``met_anom``   max per-metric mean-change ratio incident vs baseline

Plus **case-level modality-presence flags** ``has_logs`` / ``has_traces`` /
``has_metrics``: a ``0.0`` feature is otherwise ambiguous (no signal vs no data),
and the ablation showed making presence explicit lifts LOSO 48.9%->60.0%.

The feature *math* is pure (``log_features`` / ``trace_features`` /
``metric_features`` / ``assemble_features`` take plain records) so it is
unit-testable without a database; :func:`compute_features` is the thin DB layer.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.rca.abstention import (
    WindowAnomaly,
    log_rate_anomaly,
    magnitude_anomaly,
    window_anomaly,
)
from src.db.models import LogEntry, MetricSample, TraceSpan

# Error levels that count toward root-cause attribution — matches the trivial
# baseline / clusterer error filter (#82).
_ERROR_LEVELS = frozenset({"error", "fatal", "critical"})

# Originating stack-trace / traceback markers across Java/Python/Go stacks. Same
# pattern as the validated multi-modal spike (scripts/spike_multimodal_rca.py).
_STACK_RE = re.compile(
    r"\n\s*at\s+\S|\bat\s[\w.$]+\([\w.$ ]*:\d+\)|Traceback \(most recent call last\)"
    r'|\n\s*File "[^"]+", line \d+|\bpanic:\s|\bgoroutine\s+\d+\s+\[|Exception in thread'
    r"|nested exception is [\w.$]+",
    re.IGNORECASE,
)

_EPS = 1e-9

# Canonical feature order for the ranker's input vector (matches the spike's
# "all + presence" variant — the 60% LOSO configuration).
FEATURE_NAMES: list[str] = [
    "log_err",
    "log_grp",
    "log_stack",
    "tr_rate",
    "tr_dur",
    "met_anom",
    "has_logs",
    "has_traces",
    "has_metrics",
]


@dataclass
class ServiceFeatures:
    """One candidate service's multi-modal features. Presence flags are
    case-level (identical across a case's services)."""

    service: str
    log_err: int = 0
    log_grp: int = 0
    log_stack: int = 0
    tr_rate: float = 0.0
    tr_dur: float = 0.0
    met_anom: float = 0.0
    has_logs: int = 0
    has_traces: int = 0
    has_metrics: int = 0

    def as_dict(self) -> dict:
        return {"service": self.service, **{f: getattr(self, f) for f in FEATURE_NAMES}}

    def vector(self, feature_names: list[str] | None = None) -> list[float]:
        """Feature values in a fixed order, for a model's input row."""
        return [float(getattr(self, f)) for f in (feature_names or FEATURE_NAMES)]


@dataclass
class FeatureTable:
    services: list[ServiceFeatures] = field(default_factory=list)
    has_logs: bool = False
    has_traces: bool = False
    has_metrics: bool = False


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    return statistics.quantiles(xs, n=20)[18] if len(xs) >= 20 else float(max(xs))


def _seconds(delta_start: datetime, delta_end: datetime) -> float:
    """Positive span length in seconds; floors at 1s so rate ratios stay finite
    for degenerate/empty windows."""
    return max((delta_end - delta_start).total_seconds(), 1.0)


def log_features(
    entries, incident_start: datetime, incident_end: datetime
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """``(err, grp_by_svc, stack)`` counts over the incident window. ``entries``
    are duck-typed rows with ``service`` / ``level`` / ``timestamp`` and a
    ``raw_message`` (falling back to ``normalized_message``) + ``fingerprint``."""
    err: Counter = Counter()
    stack: Counter = Counter()
    grp: Counter = Counter()
    for e in entries:
        s = e.service
        ts = e.timestamp
        if not s or ts is None or not (incident_start <= ts <= incident_end):
            continue
        if (e.level or "").lower() in _ERROR_LEVELS:
            err[s] += 1
            grp[(s, e.fingerprint)] += 1
        msg = getattr(e, "raw_message", None) or getattr(e, "normalized_message", None) or ""
        if msg and _STACK_RE.search(msg):
            stack[s] += 1
    grp_by_svc: dict[str, int] = defaultdict(int)
    for (s, _fp), c in grp.items():
        grp_by_svc[s] = max(grp_by_svc[s], c)
    return dict(err), dict(grp_by_svc), dict(stack)


def trace_features(
    spans, baseline_start: datetime, incident_start: datetime, incident_end: datetime
) -> tuple[dict[str, float], dict[str, float]]:
    """``(rate, dur)`` per service: span-rate and p95-duration ratios of the
    incident window vs the baseline. ``spans`` have ``service`` / ``start_time`` /
    ``duration_ms``. Only services seen in the incident window get an entry."""
    pre = _seconds(baseline_start, incident_start)
    post = _seconds(incident_start, incident_end)
    bc: Counter = Counter()
    ic: Counter = Counter()
    bd: dict[str, list[float]] = defaultdict(list)
    idur: dict[str, list[float]] = defaultdict(list)
    for sp in spans:
        s = sp.service
        ts = sp.start_time
        d = sp.duration_ms
        if not s or ts is None or d is None:
            continue
        if baseline_start <= ts < incident_start:
            bc[s] += 1
            bd[s].append(float(d))
        elif incident_start <= ts <= incident_end:
            ic[s] += 1
            idur[s].append(float(d))
    rate: dict[str, float] = {}
    dur: dict[str, float] = {}
    for s in ic:
        rate[s] = (ic[s] / post + _EPS) / (bc.get(s, 0) / pre + _EPS)
        dur[s] = (_p95(idur.get(s, [])) + 1) / (_p95(bd.get(s, [])) + 1)
    return rate, dur


def metric_features(
    samples, baseline_start: datetime, incident_start: datetime, incident_end: datetime
) -> dict[str, float]:
    """Per service, the max over its metrics of the incident-vs-baseline mean
    change ratio. ``samples`` have ``service`` / ``metric`` / ``value`` / ``ts``."""
    base: dict[tuple[str, str], list[float]] = defaultdict(list)
    inc: dict[tuple[str, str], list[float]] = defaultdict(list)
    for m in samples:
        s = m.service
        ts = m.ts
        v = m.value
        if not s or ts is None or v is None:
            continue
        key = (s, m.metric)
        if baseline_start <= ts < incident_start:
            base[key].append(float(v))
        elif incident_start <= ts <= incident_end:
            inc[key].append(float(v))
    anom: dict[str, float] = defaultdict(float)
    for key in set(base) & set(inc):
        b = base[key]
        i = inc[key]
        if not b or not i:
            continue
        bm = statistics.mean(b)
        ratio = abs(statistics.mean(i) - bm) / (abs(bm) + _EPS)
        anom[key[0]] = max(anom[key[0]], ratio)
    return dict(anom)


def assemble_features(
    err: dict[str, int],
    grp: dict[str, int],
    stack: dict[str, int],
    rate: dict[str, float],
    dur: dict[str, float],
    anom: dict[str, float],
) -> FeatureTable:
    """Merge the per-modality maps into one candidate table. Candidates are the
    union of services with error logs, incident traces, or metric anomalies —
    matching the spike's ``set(err) | set(rate) | set(anom)``."""
    has_logs = bool(err)
    has_traces = bool(rate)
    has_metrics = bool(anom)
    services = set(err) | set(rate) | set(anom)
    feats = [
        ServiceFeatures(
            service=s,
            log_err=err.get(s, 0),
            log_grp=grp.get(s, 0),
            log_stack=stack.get(s, 0),
            tr_rate=rate.get(s, 0.0),
            tr_dur=dur.get(s, 0.0),
            met_anom=anom.get(s, 0.0),
            has_logs=int(has_logs),
            has_traces=int(has_traces),
            has_metrics=int(has_metrics),
        )
        for s in sorted(services)
    ]
    return FeatureTable(
        services=feats, has_logs=has_logs, has_traces=has_traces, has_metrics=has_metrics
    )


def compute_features(
    db: Session,
    scope: str,
    *,
    incident_start: datetime,
    incident_end: datetime,
    baseline_start: datetime,
) -> FeatureTable:
    """Build the multi-modal feature table for one ``scope`` from persisted
    telemetry. Logs are read over the incident window; traces and metrics over
    ``[baseline_start, incident_end]`` so their ratios have a pre-incident base."""
    log_rows = db.execute(
        select(LogEntry).where(
            LogEntry.scope == scope,
            LogEntry.timestamp >= incident_start,
            LogEntry.timestamp <= incident_end,
        )
    ).scalars().all()
    span_rows = db.execute(
        select(TraceSpan).where(
            TraceSpan.scope == scope,
            TraceSpan.start_time >= baseline_start,
            TraceSpan.start_time <= incident_end,
        )
    ).scalars().all()
    metric_rows = db.execute(
        select(MetricSample).where(
            MetricSample.scope == scope,
            MetricSample.ts >= baseline_start,
            MetricSample.ts <= incident_end,
        )
    ).scalars().all()

    err, grp, stack = log_features(log_rows, incident_start, incident_end)
    rate, dur = trace_features(span_rows, baseline_start, incident_start, incident_end)
    anom = metric_features(metric_rows, baseline_start, incident_start, incident_end)
    return assemble_features(err, grp, stack, rate, dur, anom)


def log_rate_arm(
    entries, baseline_start: datetime, incident_start: datetime, incident_end: datetime,
    *, tau_log: float,
) -> Optional[float]:
    """Log arm of the abstention gate: ``max`` over services of the saturated
    incident-vs-baseline **error-rate** anomaly. Returns ``None`` when the scope has
    no log entries in the window at all (logs modality absent), so the gate treats
    it as "no evidence from logs", not "logs say all-clear"."""
    pre = _seconds(baseline_start, incident_start)
    post = _seconds(incident_start, incident_end)
    inc_err: Counter = Counter()
    base_err: Counter = Counter()
    seen = False
    for e in entries:
        ts, s = e.timestamp, e.service
        if ts is None or not (baseline_start <= ts <= incident_end):
            continue
        seen = True
        if (e.level or "").lower() not in _ERROR_LEVELS:
            continue
        if incident_start <= ts <= incident_end:
            inc_err[s] += 1
        elif baseline_start <= ts < incident_start:
            base_err[s] += 1
    if not seen:
        return None
    services = set(inc_err) | set(base_err)
    if not services:
        return 0.0  # logs present but no errors either window → no anomaly
    return max(
        log_rate_anomaly(inc_err.get(s, 0) / post, base_err.get(s, 0) / pre, tau_log)
        for s in services
    )


def metric_arm(
    samples, baseline_start: datetime, incident_start: datetime, incident_end: datetime,
    *, tau_metric: float,
) -> Optional[float]:
    """Metric arm of the abstention gate: ``max`` over services of the saturated
    per-service metric mean-change magnitude. ``None`` when no metrics are present."""
    anom = metric_features(samples, baseline_start, incident_start, incident_end)
    # metric_features already returns per-service max change; None if no metric rows
    # contributed to a ratio. Distinguish "no metrics at all" from "no change".
    if not samples:
        return None
    return max((magnitude_anomaly(c, tau_metric) for c in anom.values()), default=0.0)


def compute_window_anomaly(
    db: Session,
    scope: str,
    *,
    incident_start: datetime,
    incident_end: datetime,
    baseline_start: datetime,
    tau_log: float,
    tau_metric: float,
    service: Optional[str] = None,
    environment: Optional[str] = None,
    ingestion_job_id=None,
) -> WindowAnomaly:
    """Abstention gate score (#79) for a ``(scope, window)`` — **logs + metrics
    only** (traces are a localisation signal, not a detector; see
    ``docs/eval-abstention.md``). Fuses the per-modality saturated anomalies by
    ``max`` over available modalities; a missing modality is absent, not 0.

    Filters match the view being explained (``service`` / ``environment`` /
    ``ingestion_job_id``), so the gate judges the same data as the narrative — in
    particular job-scoping (the CLI's normal mode) keeps the incident/baseline error
    counts from mixing other ingests in the scope (no cross-job baseline pollution).
    ``MetricSample`` has no ``environment`` column, so the metric arm is filtered by
    ``service`` / ``ingestion_job_id`` only."""
    log_where = [
        LogEntry.scope == scope,
        LogEntry.timestamp >= baseline_start,
        LogEntry.timestamp <= incident_end,
    ]
    metric_where = [
        MetricSample.scope == scope,
        MetricSample.ts >= baseline_start,
        MetricSample.ts <= incident_end,
    ]
    if service:
        log_where.append(LogEntry.service == service)
        metric_where.append(MetricSample.service == service)
    if environment:
        log_where.append(LogEntry.environment == environment)
    if ingestion_job_id is not None:
        log_where.append(LogEntry.ingestion_job_id == ingestion_job_id)
        metric_where.append(MetricSample.ingestion_job_id == ingestion_job_id)
    log_rows = db.execute(select(LogEntry).where(*log_where)).scalars().all()
    metric_rows = db.execute(select(MetricSample).where(*metric_where)).scalars().all()
    logs = log_rate_arm(log_rows, baseline_start, incident_start, incident_end, tau_log=tau_log)
    metrics = metric_arm(metric_rows, baseline_start, incident_start, incident_end, tau_metric=tau_metric)
    return window_anomaly(logs=logs, metrics=metrics)
