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

from sqlalchemy import select
from sqlalchemy.orm import Session

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


def detect_absence(
    spans,
    baseline_start: datetime,
    incident_start: datetime,
    incident_end: datetime,
    *,
    available_services: set,
    min_baseline_spans: int = 5,
    min_expected_incident: float = 5.0,
    collapse_fraction: float = 0.2,
) -> dict[str, float]:
    """Absence-derived candidates (#184, Phase F): services whose span traffic was **established in the
    baseline and collapsed in the incident** — the silent-failure coverage gap the normal
    incident-window features can't see (``trace_features`` only entries services present in the
    incident). Returns ``{service: collapse_strength in (0, 1]}``.

    These are **candidates to investigate, not proven disappearances**: from traces alone a span
    collapse cannot be told apart from a trace-exporter/collector gap for that service. Callers must
    present the result as an ambiguous *"went silent in traces"* signal, not a confirmed causal
    disappearance. An observation is emitted only under all three of #184's gates, else UNKNOWN
    (empty):

    1. **Baselined** (Invariant 2): a service needs ≥ ``min_baseline_spans`` baseline spans, so a
       service that was *never seen* — missing telemetry — can never become a disappearance signal.
    2. **Service up in the incident**: the service itself must be in ``available_services`` — an
       independent per-service signal (the caller passes services still emitting incident *metrics*).
       This rules out a *full* outage (service gone → nothing to interpret) and scope-wide false
       positives (an unrelated healthy service does not make the target available), but it does **not**
       prove the trace signal was collectable — hence the candidate is ambiguous, not proof.
    3. **Expected**: the baseline rate must predict a meaningful incident count
       (``base_rate × incident_seconds ≥ min_expected_incident``). Five spans spread over a 24h
       baseline predict ~zero spans in a 5-minute incident, so zero is the ordinary outcome, not a
       collapse. Only then is a drop to ``≤ collapse_fraction`` of the baseline rate a candidate."""
    pre = _seconds(baseline_start, incident_start)
    post = _seconds(incident_start, incident_end)
    bc: Counter = Counter()
    ic: Counter = Counter()
    for sp in spans:
        s = sp.service
        ts = sp.start_time
        if not s or ts is None:
            continue
        if baseline_start <= ts < incident_start:
            bc[s] += 1
        elif incident_start <= ts <= incident_end:
            ic[s] += 1
    absent: dict[str, float] = {}
    for s, base in bc.items():
        if s not in available_services:  # (2) target not independently available this incident -> UNKNOWN
            continue
        if base < min_baseline_spans:  # (1) not baselined enough to be "expected"
            continue
        base_rate = base / pre
        if base_rate * post < min_expected_incident:  # (3) too sparse to expect incident traffic
            continue
        inc_rate = ic.get(s, 0) / post
        if inc_rate <= collapse_fraction * base_rate:
            absent[s] = round(1.0 - inc_rate / base_rate, 4)
    return absent


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


# OTLP span status: 2 = ERROR (OTel Demo emits it; RCAEval traces are all UNSET/OK,
# so error-status is a bonus where present and latency carries the rest).
_ERROR_STATUS = frozenset({"2", "error", "status_code_error"})


def _is_error_status(sc) -> bool:
    return sc is not None and str(sc).strip().lower() in _ERROR_STATUS


def trace_symptoms(
    rows,
    incident_start: float,
    incident_end: float,
) -> dict[str, tuple[float, float]]:
    """Per-service trace *symptom* evidence for the propagation reranker (#118): the
    onset and strength of trouble seen in traces, so the reranker still fires where
    logs are silent (the OTel Demo emits almost no error logs, but its spans carry
    ERROR status).

    A span is a symptom when it carries **ERROR status** — the trace analog of an
    error log. Latency-excursion was evaluated as an additional trigger and dropped:
    on RCAEval it materially regressed resource faults (a resource fault balloons
    latency across every downstream service, so "earliest excursion" is noise), while
    error-status is clean. This keeps RCAEval — which has essentially no error-status
    spans — on its log-only behaviour, and lets OTel (which does emit ERROR status)
    fire.

    ``rows`` is an iterable of ``(service, ts_sec, status_code)`` — epoch seconds so
    the pipeline (datetimes) and the eval harness (parquet millis) share one
    definition. Returns ``service -> (onset_sec, magnitude)`` (first error span's time,
    error-span count); services with no error span are omitted.
    """
    from collections import defaultdict

    onset: dict[str, float] = {}
    mag: dict[str, int] = defaultdict(int)
    for service, ts_sec, status_code in rows:
        if not service or ts_sec is None or not _is_error_status(status_code):
            continue
        if not (incident_start <= ts_sec <= incident_end):
            continue
        ts = float(ts_sec)
        mag[service] += 1
        if service not in onset or ts < onset[service]:
            onset[service] = ts
    return {s: (onset[s], float(mag[s])) for s in mag}


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
