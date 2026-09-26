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

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.core.rca.expectations import ExpectedObservation, ObservationModel
from src.core.rca.hypothesis import Hypothesis, Kind, from_observation_model
from src.core.rca.observable import Observable, State, observed
from src.core.rca.partition import discretize_rate, discretize_ratio


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


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


def structural_signals(
    spans: list, metrics: list, window_start: datetime
) -> tuple[dict[str, ServiceSignal], set[tuple[str, str]]]:
    """The single structural-model input builder shared by the product path
    (:func:`~src.core.rca.structural.build_structural_view`) and the shadow eval
    (``src.eval.structural_shadow.shadow_result``), so both run one model. ``spans`` and ``metrics`` span
    the baseline *and* incident windows. Returns ``(signals, edges)``: span-derived and metric-derived
    ``sig`` merged by :func:`combine_signals`, and the **incident** call graph."""
    signals = combine_signals(summarize_spans(spans, window_start), summarize_metrics(metrics, window_start))
    return signals, call_edges(spans, since=window_start)


def build_observables(signals: dict[str, ServiceSignal]) -> list[Observable]:
    """One ``sig:{service}`` observable per service whose combined signal is *proven* PRESENT or ABSENT
    (three-valued). A service whose ``sig`` is UNKNOWN (a branch unmeasured) is omitted — its
    coordinate stays UNKNOWN, never a fabricated OBSERVED ABSENT."""
    return [
        observed(f"sig:{svc}", sig.sig_state)
        for svc, sig in sorted(signals.items())
        if sig.sig_state is not None
    ]


def _callees(service: str, edges: set[tuple[str, str]]) -> set[str]:
    return {callee for caller, callee in edges if caller == service}


def service_universe(signals: dict[str, ServiceSignal], span_services: set[str]) -> set[str]:
    """Every service seen in the telemetry — metric-bearing services **and** all services seen in spans
    (including root-only spans with no parent edge)."""
    return set(signals) | set(span_services)


def build_hypotheses(
    signals: dict[str, ServiceSignal], edges: set[tuple[str, str]]
) -> list[Hypothesis]:
    """Candidate ``process`` hypotheses: anomalous services, plus a service's callees when its fault is
    not already explained by a visible (anomalous) callee — so a silent downstream root is still
    generated. Each predicts only its own ``sig`` present; dependency direction is **not** a hard rule
    (an anomalous callee doesn't exclude its caller as root — that stays soft ranking, Phase G)."""
    anomalous = {s for s, sig in signals.items() if sig.anomalous}
    candidates = set(anomalous)
    for s in anomalous:
        callee_set = _callees(s, edges)
        if not any(c in anomalous for c in callee_set):  # fault unexplained by a visible callee
            candidates |= callee_set
    return [
        from_observation_model(
            f"process:{svc}", Kind.PROCESS, svc,
            ObservationModel(expected=(ExpectedObservation(f"sig:{svc}", State.PRESENT),)),
        )
        for svc in sorted(candidates)
    ]
