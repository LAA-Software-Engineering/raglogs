"""The minimal, source-agnostic structural observation model (#177) — shared by the offline shadow
eval (#186, Phase H2) and the live structural view (#187, Phase I).

This is the *validated* A–E adapter H2 measured: build **availability-honest** ``sig:{service}``
observables and a propagation-aware candidate hypothesis set from a case's own telemetry, so both the
shadow eval and the product path run the **same** model (no divergent copy). It operates on duck-typed
records — anything with the right attributes — so it works on eval ``ParsedSpan``/``ParsedMetricSample``
records and on the live ``TraceSpan``/``MetricSample`` ORM rows alike.

Two deliberate soundness choices (see H2, #186):

- **Availability is honest.** A ``sig:{service}`` observable is OBSERVED only when an incident
  error/latency signal was actually measured; a service with no incident measurement stays UNKNOWN,
  never a synthesized OBSERVED ABSENT.
- **No unsound hard rule.** An anomalous callee does not logically exclude its caller as the root, so
  each hypothesis predicts only its own ``sig`` — the partition here largely *enumerates* candidates
  rather than doing strong structural elimination (which is why the honest metric is recall, and why
  the live view frequently, and correctly, terminates in UNCERTAIN).
"""
from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple, Optional

from src.core.rca.expectations import ExpectedObservation, ObservationModel
from src.core.rca.hypothesis import Hypothesis, Kind, from_observation_model
from src.core.rca.observable import Observable, State, observed
from src.core.rca.partition import discretize_rate, discretize_ratio


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def _mean(xs: list[float]) -> float:
    """For periodic gauge samples (already aggregates), unlike raw per-request durations."""
    return sum(xs) / len(xs) if xs else 0.0


# OTLP span status codes: 0 = UNSET, 1 = OK, 2 = ERROR. Only an EXPLICIT status is a measurement:
# UNSET (or null / missing) says nothing about whether the request succeeded, so it is excluded from
# the error-rate denominator entirely — it is neither a success nor a failure. Counting UNSET as
# success would turn missing telemetry into a measured error rate of zero (and, with latency normal,
# into OBSERVED ABSENT), and would dilute sparse ERROR spans below any rate cutoff.
_ERROR_STATUS = frozenset({"2", "error", "status_code_error"})
_OK_STATUS = frozenset({"1", "ok", "status_code_ok"})


def _status_class(status) -> Optional[str]:
    """``"error"`` / ``"ok"`` for an explicit OTLP status, ``None`` for UNSET / null / unknown."""
    if status is None:
        return None
    s = str(status).strip().lower()
    if s in _ERROR_STATUS:
        return "error"
    if s in _OK_STATUS:
        return "ok"
    return None


@dataclass(frozen=True)
class ServiceSignal:
    """A service's incident-vs-baseline summary. ``sig_state`` is **three-valued**: ``PRESENT`` when a
    *measured* branch (error or latency) proves an anomaly, ``ABSENT`` only when **both** branches were
    measured and normal, and ``None`` (UNKNOWN) otherwise — a missing branch never fabricates absence.
    ``measured`` = ``sig_state is not None``; ``anomalous`` = ``sig_state == PRESENT``."""

    service: str
    error_rate: float
    latency_ratio: float
    error_measured: bool
    latency_measured: bool
    sig_state: Optional[str]

    @property
    def measured(self) -> bool:
        return self.sig_state is not None

    @property
    def anomalous(self) -> bool:
        return self.sig_state == State.PRESENT


def summarize_metrics(samples: list, window_start: datetime) -> dict[str, ServiceSignal]:
    """Summarize metric-sample records into a per-service signal. Samples at/after ``window_start`` are
    the incident; earlier ones the baseline. The combined ``sig`` (error present **or** latency ≥2×) is
    computed with **three-valued** availability: an error branch is measured when incident
    ``error_rate`` exists; a latency branch only when both incident **and** baseline ``latency_ms``
    exist (a ratio needs both). ``sig`` is ``PRESENT`` if a measured branch is anomalous, ``ABSENT``
    only if both branches are measured-and-normal, else UNKNOWN. Records are duck-typed on
    ``service`` / ``value`` / ``ts`` / ``metric``."""
    inc: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    base: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for s in samples:
        if s.service is None or s.value is None or s.ts is None:
            continue
        bucket = inc if s.ts >= window_start else base
        bucket[s.service][s.metric].append(float(s.value))

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    def all_valid_rates(xs: list[float]) -> bool:
        return all(math.isfinite(x) and 0.0 <= x <= 1.0 for x in xs)

    def all_nonneg(xs: list[float]) -> bool:
        return all(math.isfinite(x) and x >= 0.0 for x in xs)

    signals: dict[str, ServiceSignal] = {}
    for svc in sorted(set(inc) | set(base)):
        inc_err = inc[svc].get("error_rate", [])
        inc_lat = inc[svc].get("latency_ms", [])
        base_lat = base[svc].get("latency_ms", [])
        base_lat_mean = mean(base_lat)

        # Validate every RAW sample, not just the aggregate — an in-range mean does not prove valid
        # inputs (e.g. incident latency [-10, 30] averages to a normal-looking 10). Any malformed
        # sample leaves that branch UNKNOWN (unmeasured), never averaged into false-normal evidence.
        # A rate must be finite in [0,1]; a latency needs valid non-negative incident + positive
        # baseline samples (30/0 is UNKNOWN, not normal).
        error_measured = bool(inc_err) and all_valid_rates(inc_err)
        latency_measured = (
            bool(inc_lat) and all_nonneg(inc_lat)
            and bool(base_lat) and all_nonneg(base_lat) and base_lat_mean > 0
        )
        err = mean(inc_err)
        ratio = mean(inc_lat) / base_lat_mean if latency_measured else 1.0

        error_present = error_measured and discretize_rate(err) == State.PRESENT
        latency_high = latency_measured and discretize_ratio(ratio) == State.HIGH
        if error_present or latency_high:          # a measured branch proves the anomaly
            sig_state: Optional[str] = State.PRESENT
        elif error_measured and latency_measured:  # both measured and normal -> proven absent
            sig_state = State.ABSENT
        else:                                       # some branch unmeasured, nothing proves present
            sig_state = None
        signals[svc] = ServiceSignal(svc, err, ratio, error_measured, latency_measured, sig_state)
    return signals


def summarize_spans(spans: list, window_start: datetime) -> dict[str, ServiceSignal]:
    """Per-service ``sig`` from raw **span** records (#209 M1) — latency-first, plus sparse error-status.

    On real OTLP, latency is the workhorse: almost every span carries ``duration_ms``, while an explicit
    status is rare, so a service with spans but no error/latency *metrics* is `UNKNOWN` under the
    metric-only model. Deriving ``sig`` from spans gives those services an observable. Three-valued
    availability, mirroring :func:`summarize_metrics`: PRESENT if a *measured* branch is anomalous,
    ABSENT only when **both** branches are measured-and-normal, else UNKNOWN — missing telemetry never
    fabricates ABSENT. Records are duck-typed on ``service`` / ``start_time`` / ``duration_ms`` /
    ``status_code``.

    * **Error branch** — measured only over spans with an **explicit** OTLP status (OK or ERROR); the
      rate is ``ERROR / (OK + ERROR)``. UNSET / null status is not a measurement and is excluded from
      the denominator, so a service whose incident spans are all UNSET has an *unmeasured* error branch
      (like a service with no ``error_rate`` metric samples), and sparse ERROR spans are not diluted by
      a denominator of UNSET spans.
    * **Latency branch** — the **median** incident duration over the median baseline duration (needs
      both). Raw per-request durations are heavy-tailed; with the ≥2× discretizer, a mean (or a tail
      quantile at small n) lets a single slow request flag a whole service. Only valid non-negative
      finite durations are used."""
    inc_dur: dict[str, list[float]] = defaultdict(list)
    base_dur: dict[str, list[float]] = defaultdict(list)
    inc_ok: Counter = Counter()
    inc_err: Counter = Counter()
    for sp in spans:
        s = getattr(sp, "service", None)
        ts = getattr(sp, "start_time", None)
        if not s or ts is None:
            continue
        dur = getattr(sp, "duration_ms", None)
        valid_dur = dur if (isinstance(dur, (int, float)) and math.isfinite(dur) and dur >= 0) else None
        if ts >= window_start:
            status = _status_class(getattr(sp, "status_code", None))
            if status == "error":
                inc_err[s] += 1
            elif status == "ok":
                inc_ok[s] += 1
            if valid_dur is not None:
                inc_dur[s].append(float(valid_dur))
        elif valid_dur is not None:
            base_dur[s].append(float(valid_dur))

    signals: dict[str, ServiceSignal] = {}
    for svc in sorted(set(inc_ok) | set(inc_err) | set(inc_dur) | set(base_dur)):
        explicit = inc_ok.get(svc, 0) + inc_err.get(svc, 0)
        error_measured = explicit > 0
        err_rate = inc_err.get(svc, 0) / explicit if explicit else 0.0
        base_med = _median(base_dur[svc])
        latency_measured = bool(inc_dur[svc]) and bool(base_dur[svc]) and base_med > 0
        ratio = _median(inc_dur[svc]) / base_med if latency_measured else 1.0

        error_present = error_measured and discretize_rate(err_rate) == State.PRESENT
        latency_high = latency_measured and discretize_ratio(ratio) == State.HIGH
        if error_present or latency_high:
            sig_state: Optional[str] = State.PRESENT
        elif error_measured and latency_measured:
            sig_state = State.ABSENT
        else:
            sig_state = None
        signals[svc] = ServiceSignal(svc, err_rate, ratio, error_measured, latency_measured, sig_state)
    return signals


def combine_signals(*sources: dict[str, ServiceSignal]) -> dict[str, ServiceSignal]:
    """Merge per-service signals from several modalities (spans + metrics) with **PRESENT > ABSENT >
    UNKNOWN** priority: a service is anomalous if *any* modality proves it, measured-normal only if
    some modality measured it normal and none proved it anomalous, else UNKNOWN. Never fabricates
    ABSENT (a service UNKNOWN in every modality stays UNKNOWN).

    The merged record is the **witness** modality's record, verbatim — the first PRESENT one, else the
    first ABSENT one, else the first UNKNOWN one. Its rates, ratio and measured-flags travel together:
    numbers from a modality whose state lost are never attached to the winning state, and flags are
    never OR'd across modalities (that would claim a branch measurement the witness did not make)."""
    services: set[str] = set()
    for src in sources:
        services |= set(src)
    out: dict[str, ServiceSignal] = {}
    for svc in sorted(services):
        sigs = [src[svc] for src in sources if svc in src]
        witness = (
            next((s for s in sigs if s.sig_state == State.PRESENT), None)
            or next((s for s in sigs if s.sig_state == State.ABSENT), None)
            or sigs[0]
        )
        out[svc] = witness
    return out


def call_edges(spans: list, since: Optional[datetime] = None) -> set[tuple[str, str]]:
    """Distinct (caller_service, callee_service) edges from span records via parent links.

    A ``span_id`` is scoped to its trace, so the parent index is keyed by ``(trace_id, span_id)`` —
    keying by ``span_id`` alone would let one trace's span overwrite another's that reuses the same
    local id, inventing or dropping edges by row order. A span with no ``trace_id`` cannot be resolved
    across traces safely, so it is skipped for edge construction. Records are duck-typed on
    ``trace_id`` / ``span_id`` / ``parent_span_id`` / ``service`` (and ``start_time`` when ``since`` is
    given).

    ``since`` restricts the result to the **incident** call graph: an edge is emitted only when the
    *child* span starts at/after ``since``. The parent index is still built from every span passed, so
    a parent that started before ``since`` still resolves for an incident child — but a pair that exists
    only in the baseline never becomes an incident edge."""
    service_of = {
        (sp.trace_id, sp.span_id): sp.service
        for sp in spans if sp.trace_id and sp.span_id and sp.service
    }
    edges: set[tuple[str, str]] = set()
    for sp in spans:
        if not (sp.trace_id and sp.parent_span_id and sp.service):
            continue
        if since is not None:
            ts = getattr(sp, "start_time", None)
            if ts is None or ts < since:
                continue  # baseline (or undated) child: not part of the incident call graph
        caller = service_of.get((sp.trace_id, sp.parent_span_id))
        if caller and caller != sp.service:
            edges.add((caller, sp.service))
    return edges


@dataclass(frozen=True)
class EdgeSignal:
    """A caller→callee call edge's incident-vs-baseline summary (#209 M2a) — the per-edge observable
    ``edge:{caller}->{callee}``, kept separate from either endpoint's ``sig``. Same three-valued
    semantics as :class:`ServiceSignal`, but measured on the **caller's** spans of the operations that
    call ``callee``: an unreachable or broken callee is visible here even when it emits nothing itself."""

    caller: str
    callee: str
    error_rate: float
    latency_ratio: float
    error_measured: bool
    latency_measured: bool
    sig_state: Optional[str]

    @property
    def id(self) -> str:
        return edge_observable_id(self.caller, self.callee)


def edge_observable_id(caller: str, callee: str) -> str:
    return f"edge:{caller}->{callee}"


def learn_call_targets(spans: list, *, dominance: float = 0.9) -> dict[tuple[str, str], str]:
    """``{(caller_service, operation): callee_service}`` learned from **observed** parent→child
    relations, never from operation names. A ``(caller, operation)`` pair maps to ``S`` only when at
    least ``dominance`` of its cross-service children are in ``S``; an operation that fans out to several
    services (or has no observed cross-service child) is ambiguous and gets no mapping — no evidence
    rather than a guessed target. Parent lookup is keyed by ``(trace_id, span_id)`` like
    :func:`call_edges`."""
    by_id = {
        (sp.trace_id, sp.span_id): sp
        for sp in spans if sp.trace_id and sp.span_id and sp.service
    }
    counts: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for sp in spans:
        if not (sp.trace_id and sp.parent_span_id and sp.service):
            continue
        parent = by_id.get((sp.trace_id, sp.parent_span_id))
        if parent is None or parent.service == sp.service:
            continue
        op = getattr(parent, "operation", None)
        if op:
            counts[(parent.service, op)][sp.service] += 1
    targets: dict[tuple[str, str], str] = {}
    for key, cnt in counts.items():
        callee, n = cnt.most_common(1)[0]
        if n / sum(cnt.values()) >= dominance:  # >=0.9 dominance cannot tie, so this is deterministic
            targets[key] = callee
    return targets


def summarize_edges(
    spans: list, window_start: datetime, *, dominance: float = 0.9
) -> dict[tuple[str, str], EdgeSignal]:
    """Per call edge ``(caller, callee)``, summarize the caller's spans of the operations mapped to
    ``callee`` by :func:`learn_call_targets` (#209 M2a).

    * **Error branch** — explicit OTLP status only, ``ERROR / (OK + ERROR)`` over the incident spans;
      UNSET is excluded, exactly as in :func:`summarize_spans`. Measured iff an explicit status exists.
    * **Latency branch** — median incident duration over median baseline duration; needs both.

    PRESENT if a measured branch is anomalous, ABSENT only if both are measured-and-normal, else
    UNKNOWN. Only edges with **at least one incident call** are returned: an edge that was simply not
    called during the incident has no observation (absence of calls is not evidence). A failed call
    whose callee emitted no span at all is exactly the case this observable exists for."""
    targets = learn_call_targets(spans, dominance=dominance)
    inc_calls: Counter = Counter()
    inc_ok: Counter = Counter()
    inc_err: Counter = Counter()
    inc_dur: dict[tuple[str, str], list[float]] = defaultdict(list)
    base_dur: dict[tuple[str, str], list[float]] = defaultdict(list)
    for sp in spans:
        if not sp.service:
            continue
        callee = targets.get((sp.service, getattr(sp, "operation", None)))
        ts = getattr(sp, "start_time", None)
        if callee is None or ts is None:
            continue
        key = (sp.service, callee)
        dur = getattr(sp, "duration_ms", None)
        valid_dur = dur if (isinstance(dur, (int, float)) and math.isfinite(dur) and dur >= 0) else None
        if ts >= window_start:
            inc_calls[key] += 1
            status = _status_class(getattr(sp, "status_code", None))
            if status == "error":
                inc_err[key] += 1
            elif status == "ok":
                inc_ok[key] += 1
            if valid_dur is not None:
                inc_dur[key].append(float(valid_dur))
        elif valid_dur is not None:
            base_dur[key].append(float(valid_dur))

    out: dict[tuple[str, str], EdgeSignal] = {}
    for key in sorted(inc_calls):
        explicit = inc_ok[key] + inc_err[key]
        error_measured = explicit > 0
        err_rate = inc_err[key] / explicit if explicit else 0.0
        base_med = _median(base_dur[key])
        latency_measured = bool(inc_dur[key]) and bool(base_dur[key]) and base_med > 0
        ratio = _median(inc_dur[key]) / base_med if latency_measured else 1.0
        error_present = error_measured and discretize_rate(err_rate) == State.PRESENT
        latency_high = latency_measured and discretize_ratio(ratio) == State.HIGH
        if error_present or latency_high:
            state: Optional[str] = State.PRESENT
        elif error_measured and latency_measured:
            state = State.ABSENT
        else:
            state = None
        out[key] = EdgeSignal(key[0], key[1], err_rate, ratio, error_measured, latency_measured, state)
    return out


@dataclass(frozen=True)
class UtilSignal:
    """A service's resource-utilization summary (#209 M2b) — the observable ``util:{service}:{resource}``,
    its own coordinate family, never folded into ``sig:{service}``. It recovers *locally silent resource
    faults*: a CPU-saturated or event-loop-saturated service whose requests still look normal in traces.
    ``ratio`` is the witness metric's incident/baseline ratio; ``sig_state`` is three-valued."""

    service: str
    resource: str
    ratio: float
    measured: bool
    sig_state: Optional[str]

    @property
    def id(self) -> str:
        return util_observable_id(self.service, self.resource)


def util_observable_id(service: str, resource: str) -> str:
    return f"util:{service}:{resource}"


_SELF_TELEMETRY_PREFIXES = ("otel.sdk.", "otelcol")


def util_metric_class(metric: Optional[str]) -> Optional[tuple[str, str]]:
    """Map a metric name to ``(resource, required_instrument)`` by OpenTelemetry semantic-convention
    **name shape**, or ``None``. The name only says *which resource* the metric is about and *which
    instrument it must be* for the reducer to apply; whether it actually **is** that instrument is read
    from the sample's ``metric_type``, never inferred from the name:

    * last dotted segment ends in ``utilization`` → the preceding segment is the resource
      (``jvm.cpu.recent_utilization`` → ``cpu``, ``nodejs.eventloop.utilization`` → ``eventloop``,
      ``system.memory.utilization`` → ``memory``); required instrument ``"gauge"``;
    * name ends ``.cpu.time`` → resource ``cpu``; required instrument ``"counter"`` (a cumulative
      monotonic sum).

    Collector/SDK self-telemetry (``otel.sdk.*``, ``otelcol*``) is excluded: it measures the telemetry
    pipeline, not the service, and moves whenever a service simply emits more spans."""
    if not metric or metric.startswith(_SELF_TELEMETRY_PREFIXES):
        return None
    parts = metric.split(".")
    if len(parts) >= 2 and parts[-1].endswith("utilization"):
        return parts[-2], "gauge"
    if metric.endswith(".cpu.time"):
        return "cpu", "counter"
    return None


def _counter_rate(points: list[tuple[datetime, float]]) -> Optional[float]:
    if len(points) < 2:
        return None
    dt = (points[-1][0] - points[0][0]).total_seconds()
    return (points[-1][1] - points[0][1]) / dt if dt > 0 else None


def _series_key(attributes) -> Optional[str]:
    """Canonical series identity of a sample, or ``None`` when the source never recorded it."""
    if attributes is None:
        return None
    return json.dumps(attributes, sort_keys=True, default=str)


def _instrument(metric_type: Optional[str]) -> str:
    """``MetricSample`` convention: a null ``metric_type`` is a gauge."""
    return metric_type or "gauge"


def summarize_utilization(samples: list, window_start: datetime) -> dict[tuple[str, str], UtilSignal]:
    """Per ``(service, resource)`` utilization observable from metric-sample records (#209 M2b), duck-typed
    on ``service`` / ``metric`` / ``value`` / ``ts`` / ``metric_type`` / ``attributes``.

    **A measurement requires one verified series.** A metric's samples form a series only when the
    source recorded series identity (``attributes`` — datapoint attributes such as ``cpu.mode`` plus
    the reporting instance). A metric is **measured only if** every one of its samples carries identity,
    they all belong to **exactly one** series, no two samples share a timestamp, and the declared
    instrument (``metric_type``) is the one the reducer requires. Otherwise it is UNKNOWN:

    * no identity — the samples cannot be told apart, so a per-state instrument
      (``system.cpu.utilization`` per ``cpu.mode``, ``system.memory.utilization`` per state) or several
      reporting instances would be averaged (or, after a lossy ingest, reduced to whichever row landed
      first) into a number that is not one resource's utilization — never emitted as ABSENT;
    * several series — there are no per-mode semantics yet, so the reducer neither averages nor picks;
    * wrong instrument — a ``*.cpu.time`` that is not declared a cumulative counter is not rated.

    Reducers: a **gauge** compares incident mean / baseline mean (periodic aggregates; every raw value
    finite and ≥ 0, baseline mean > 0); a **counter** compares incident rate / baseline rate, and a
    decrease inside the series is a **reset**, so that series is UNKNOWN. Unattributed samples
    (``service`` None) and self-telemetry contribute nothing.

    Per metric: PRESENT if ratio ≥ 2× (``discretize_ratio`` HIGH), ABSENT if measured and below, else
    UNKNOWN. The ``(service, resource)`` signal takes the witness (PRESENT > ABSENT > UNKNOWN). A
    utilization *drop* is ABSENT (not saturated), not an anomaly."""
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for s in samples:
        svc, metric = getattr(s, "service", None), getattr(s, "metric", None)
        if svc is None or getattr(s, "value", None) is None or getattr(s, "ts", None) is None:
            continue
        if util_metric_class(metric) is None:
            continue
        groups[(svc, metric)].append(s)

    per_class: dict[tuple[str, str], list[tuple[Optional[str], float, bool]]] = defaultdict(list)
    for (svc, metric), rows in groups.items():
        resource, required = util_metric_class(metric)
        keys = {_series_key(getattr(r, "attributes", None)) for r in rows}
        values = [(r.ts, float(r.value)) for r in rows]
        single_series = None not in keys and len(keys) == 1
        distinct_ts = len({t for t, _ in values}) == len(values)
        right_instrument = all(_instrument(getattr(r, "metric_type", None)) == required for r in rows)
        well_formed = all(math.isfinite(v) and v >= 0 for _, v in values)
        base = sorted(p for p in values if p[0] < window_start)
        inc = sorted(p for p in values if p[0] >= window_start)

        ratio, measured = 1.0, False
        if single_series and distinct_ts and right_instrument and well_formed and base and inc:
            if required == "gauge":
                bmean = _mean([v for _, v in base])
                if bmean > 0:
                    ratio, measured = _mean([v for _, v in inc]) / bmean, True
            else:
                pts = sorted(values)
                no_reset = all(b >= a for (_, a), (_, b) in zip(pts, pts[1:]))
                rb, ri = _counter_rate(base), _counter_rate(inc)
                if no_reset and rb is not None and rb > 0 and ri is not None:
                    ratio, measured = ri / rb, True
        if not measured:
            state: Optional[str] = None
        elif discretize_ratio(ratio) == State.HIGH:
            state = State.PRESENT
        else:
            state = State.ABSENT
        per_class[(svc, resource)].append((state, ratio, measured))

    out: dict[tuple[str, str], UtilSignal] = {}
    for (svc, resource), metrics in sorted(per_class.items()):
        witness = (next((m for m in metrics if m[0] == State.PRESENT), None)
                   or next((m for m in metrics if m[0] == State.ABSENT), None)
                   or metrics[0])
        out[(svc, resource)] = UtilSignal(svc, resource, witness[1], witness[2], witness[0])
    return out


class StructuralInputs(NamedTuple):
    """Everything the structural model reads for one ``(scope, window)`` (#209): per-service ``sig``,
    the incident call graph, per-edge observables (M2a) and per-resource utilization observables (M2b)."""

    signals: dict[str, ServiceSignal]
    edges: set[tuple[str, str]]
    edge_signals: dict[tuple[str, str], EdgeSignal]
    util_signals: dict[tuple[str, str], UtilSignal]


def structural_signals(spans: list, metrics: list, window_start: datetime) -> StructuralInputs:
    """The single structural-model input builder shared by the product path
    (:func:`~src.core.rca.structural.build_structural_view`) and the shadow eval
    (``src.eval.structural_shadow.shadow_result``), so both run one model. ``spans`` and ``metrics`` span
    the baseline *and* incident windows. Returns :class:`StructuralInputs`: span-derived and
    metric-derived ``sig`` merged by :func:`combine_signals`, the **incident** call graph, the per-edge
    observables (M2a) and the utilization observables (M2b)."""
    signals = combine_signals(summarize_spans(spans, window_start), summarize_metrics(metrics, window_start))
    return StructuralInputs(
        signals=signals,
        edges=call_edges(spans, since=window_start),
        edge_signals=summarize_edges(spans, window_start),
        util_signals=summarize_utilization(metrics, window_start),
    )


def build_observables(
    signals: dict[str, ServiceSignal],
    edge_signals: Optional[dict[tuple[str, str], EdgeSignal]] = None,
    util_signals: Optional[dict[tuple[str, str], UtilSignal]] = None,
) -> list[Observable]:
    """One ``sig:{service}`` observable per service whose combined signal is *proven* PRESENT or ABSENT
    (three-valued), plus one ``edge:{caller}->{callee}`` per proven call edge and one
    ``util:{service}:{resource}`` per proven utilization class. Anything UNKNOWN is omitted — its
    coordinate stays UNKNOWN, never a fabricated OBSERVED ABSENT."""
    obs = [
        observed(f"sig:{svc}", sig.sig_state)
        for svc, sig in sorted(signals.items())
        if sig.sig_state is not None
    ]
    for _key, e in sorted((edge_signals or {}).items()):
        if e.sig_state is not None:
            obs.append(observed(e.id, e.sig_state))
    for _key, u in sorted((util_signals or {}).items()):
        if u.sig_state is not None:
            obs.append(observed(u.id, u.sig_state))
    return obs


def _callees(service: str, edges: set[tuple[str, str]]) -> set[str]:
    return {callee for caller, callee in edges if caller == service}


def service_universe(signals: dict[str, ServiceSignal], span_services: set[str]) -> set[str]:
    """Every service seen in the telemetry — metric-bearing services **and** all services seen in spans
    (including root-only spans with no parent edge)."""
    return set(signals) | set(span_services)


def build_hypotheses(
    signals: dict[str, ServiceSignal],
    edges: set[tuple[str, str]],
    edge_signals: Optional[dict[tuple[str, str], EdgeSignal]] = None,
    util_signals: Optional[dict[tuple[str, str], UtilSignal]] = None,
) -> list[Hypothesis]:
    """Candidate ``process`` hypotheses: anomalous services, plus a service's callees when its fault is
    not already explained by a visible (anomalous) callee — so a silent downstream root is still
    generated — plus (#209 M2a) the **callee of every PRESENT edge**, plus (#209 M2b) **every service with
    a PRESENT utilization observable** (a locally silent resource fault). Each hypothesis predicts its own
    ``sig`` present and, when given, every incoming ``edge:{caller}->{svc}`` and every
    ``util:{svc}:{resource}`` present. All expectations are soft; dependency direction is **not** a hard
    rule (an anomalous callee doesn't exclude its caller as root — that stays soft ranking)."""
    anomalous = {s for s, sig in signals.items() if sig.anomalous}
    candidates = set(anomalous)
    for s in anomalous:
        callee_set = _callees(s, edges)
        if not any(c in anomalous for c in callee_set):  # fault unexplained by a visible callee
            candidates |= callee_set
    expected_extra: dict[str, list[str]] = defaultdict(list)
    for e in (edge_signals or {}).values():
        expected_extra[e.callee].append(e.id)
        if e.sig_state == State.PRESENT:
            candidates.add(e.callee)
    for u in (util_signals or {}).values():
        expected_extra[u.service].append(u.id)
        if u.sig_state == State.PRESENT:
            candidates.add(u.service)
    return [
        from_observation_model(
            f"process:{svc}", Kind.PROCESS, svc,
            ObservationModel(expected=(
                ExpectedObservation(f"sig:{svc}", State.PRESENT),
                *(ExpectedObservation(oid, State.PRESENT) for oid in sorted(expected_extra[svc])),
            )),
        )
        for svc in sorted(candidates)
    ]
