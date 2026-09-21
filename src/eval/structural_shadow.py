"""Structural shadow evaluation — the minimal A–E bridge (#186, Phase H2 of the #177 epic).

Phase H1 (#201) bucketed the *existing* pipeline's failures. This runs the A–E structural core
(#178–#183) on **real** eval telemetry, **offline / in shadow**, and measures **candidate recall** —
does a propagation-aware hypothesis generator, fed availability-honest observables, produce a small
hypothesis set that *retains the true cause*? It changes **nothing** in the product explain path.

**Scope, honestly.** This measures *truth retention / candidate recall*, **not** structural
correctness. #186's ``struct_ok`` additionally requires the partition (classes, signatures,
``D_missing``, no false collapse) to be correct under the modeled relation; that needs a faithful
symptom-propagation observation model and per-observable availability ground truth, and is **not
measured here**. The full structural packet is preserved on each result so a future scorer can check
it. This bridge deliberately makes two *sound* choices the first cut got wrong:

- **Availability is honest**: a ``sig:{service}`` observable is emitted only when an incident
  error/latency signal was actually measured; a service with no incident measurement (baseline-only,
  or only unrelated OTLP counters) stays **UNKNOWN**, never a synthesized OBSERVED ABSENT.
- **No unsound hard rule**: an anomalous callee does **not** logically exclude its caller as the root
  (a caller can overload/misuse a callee, or a multi-fault incident). Dependency direction is *not*
  fed into the hard-contradiction channel; it is left to soft ranking (Phase G). So each hypothesis
  predicts only its own ``sig`` — the partition here largely enumerates candidates rather than doing
  strong structural elimination, which is exactly why the honest metric is recall, not ``struct_ok``.

The model: ``sig:{service}`` = ``PRESENT`` when the service is anomalous (error present or latency ≥2×
baseline, Phase D discretization) else ``ABSENT``, OBSERVED only when measured. Candidate hypotheses
are the anomalous services plus, when a service's fault is not already explained by a visible
anomalous callee, its callees — so a silent downstream root is still generated (the coverage gap H1
measured).
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.rca.expectations import ExpectedObservation, ObservationModel
from src.core.rca.hypothesis import Kind, from_observation_model
from src.core.rca.observable import Observable, State, observed
from src.core.rca.outcome import Outcome, resolve
from src.core.rca.partition import discretize_rate, discretize_ratio, partition
from src.eval.case import EvalCase


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
    """Summarize ``ParsedMetricSample`` records into a per-service signal. Samples at/after
    ``window_start`` are the incident; earlier ones the baseline. The combined ``sig`` (error present
    **or** latency ≥2×) is computed with **three-valued** availability: an error branch is measured
    when incident ``error_rate`` exists; a latency branch is measured only when both incident **and**
    baseline ``latency_ms`` exist (a ratio needs both). ``sig`` is ``PRESENT`` if a measured branch is
    anomalous, ``ABSENT`` only if both branches are measured-and-normal, else UNKNOWN."""
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


def call_edges(spans: list) -> set[tuple[str, str]]:
    """Distinct (caller_service, callee_service) edges from ``ParsedSpan`` records via parent links.

    A ``span_id`` is scoped to its trace, so the parent index is keyed by ``(trace_id, span_id)`` —
    keying by ``span_id`` alone would let one trace's span overwrite another's that reuses the same
    local id, inventing or dropping edges by row order. A span with no ``trace_id`` cannot be resolved
    across traces safely, so it is skipped for edge construction."""
    service_of = {
        (sp.trace_id, sp.span_id): sp.service
        for sp in spans if sp.trace_id and sp.span_id and sp.service
    }
    edges: set[tuple[str, str]] = set()
    for sp in spans:
        if not (sp.trace_id and sp.parent_span_id and sp.service):
            continue
        caller = service_of.get((sp.trace_id, sp.parent_span_id))
        if caller and caller != sp.service:
            edges.add((caller, sp.service))
    return edges


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
    """Every service seen in the case's telemetry — metric-bearing services **and** all services seen
    in spans (including root-only spans with no parent edge). This is the denominator for
    ``candidate_ratio`` so "enumerate everything" is exactly 1.0 and matches the telemetry-wide meaning."""
    return set(signals) | set(span_services)


def build_hypotheses(signals: dict[str, ServiceSignal], edges: set[tuple[str, str]]):
    """Candidate ``process`` hypotheses: anomalous services, plus a service's callees when its fault
    is not already explained by a visible (anomalous) callee — so a silent downstream root is still
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


@dataclass(frozen=True)
class ClassInfo:
    """A surviving class projected for later scoring: member localizations, the prediction signature,
    and the missing distinguishers. Preserved so a future real ``struct_ok`` can check the partition."""

    localizations: tuple[str, ...]
    signature: tuple[tuple[str, str], ...]
    d_missing: frozenset[str]


@dataclass(frozen=True)
class ShadowResult:
    """One case's structural shadow outcome. The metrics scored here are **recall**, not structural
    correctness: ``truth_retained`` is whether the true cause is in the generated hypothesis set;
    ``unique`` is a *structural* IDENTIFIED at the truth (no ranking). The full ``classes`` packet is
    preserved for a future ``struct_ok`` scorer."""

    case_id: str
    truth: str
    outcome: str            # Outcome value, or "no_candidates"/"no_telemetry"
    classes: tuple[ClassInfo, ...]
    localizations: tuple[str, ...]
    truth_retained: bool
    unique: bool
    abstained: bool
    n_candidates: int = 0   # size of the generated hypothesis set (localizations)
    n_services: int = 0     # services seen in the case's telemetry (the enumerate-everything baseline)

    @property
    def candidate_ratio(self) -> Optional[float]:
        """Candidate set size relative to enumerating every service (``None`` if no services seen).
        Recall near 1.0 is only meaningful if this stays well below 1.0 — otherwise it is enumeration."""
        return self.n_candidates / self.n_services if self.n_services else None


def shadow_result(case: EvalCase) -> ShadowResult:
    """Build availability-honest observables + hypotheses from the case's own telemetry, partition,
    resolve, and measure candidate recall. Requires a labeled positive case with metric + span
    sidecars."""
    from src.eval.rcaeval import load_metrics_jsonl, load_spans_jsonl

    truth = case.root_cause.service if case.root_cause else ""
    if case.metrics_path is None or case.spans_path is None:
        # No telemetry -> no localization -> an abstention (returned nothing).
        return ShadowResult(case.id, truth, "no_telemetry", (), (), False, False, True)

    signals = summarize_metrics(load_metrics_jsonl(case.metrics_path), case.window_start)
    spans = load_spans_jsonl(case.spans_path)
    edges = call_edges(spans)
    # The service universe is every service seen in telemetry — metric-bearing services and all span
    # services (incl. root-only spans with no edge) — so candidate_ratio's "1.0 = enumerate all" holds.
    n_services = len(service_universe(signals, {sp.service for sp in spans if sp.service}))
    hypotheses = build_hypotheses(signals, edges)
    if not hypotheses:
        # No hypothesis returned -> an abstention, not a zero-abstention success.
        return ShadowResult(case.id, truth, "no_candidates", (), (), False, False, True,
                            n_candidates=0, n_services=n_services)

    result = resolve(partition(hypotheses, build_observables(signals)))
    classes = tuple(ClassInfo(c.localizations, c.signature, c.d_missing) for c in result.classes)
    localizations = result.localization
    return ShadowResult(
        case_id=case.id, truth=truth, outcome=result.outcome.value,
        classes=classes, localizations=localizations,
        truth_retained=truth in localizations,
        unique=result.outcome is Outcome.IDENTIFIED and localizations == (truth,),
        abstained=not localizations,  # any empty result (incl. NO_COMPATIBLE) is an abstention
        n_candidates=len(localizations), n_services=n_services,
    )


@dataclass
class ShadowScore:
    """Aggregate structural shadow metrics over a corpus (labeled positive cases only).

    ``candidate_recall`` is the headline — the fraction of cases whose true cause the generator
    retained — but it is only meaningful alongside ``mean_candidate_ratio``: recall≈1 with the set
    being every service is enumeration, not narrowing. ``unique_rate`` is the *structural*
    identification rate (no ranking). None of these is ``struct_ok`` (full structural correctness per
    #186), which is not measured here."""

    n: int
    candidate_recall: float
    unique_rate: float
    abstention_rate: float
    mean_candidate_ratio: float   # mean |candidates| / |services| — selectivity (1.0 = enumerate all)
    mean_candidates: float
    outcome_counts: dict[str, int]


def score_shadow(results: list[ShadowResult]) -> ShadowScore:
    scored = [r for r in results if r.truth]
    n = len(scored)
    counts: dict[str, int] = defaultdict(int)
    for r in scored:
        counts[r.outcome] += 1
    ratio = lambda k: (k / n if n else 0.0)  # noqa: E731
    ratios = [r.candidate_ratio for r in scored if r.candidate_ratio is not None]
    return ShadowScore(
        n=n,
        candidate_recall=ratio(sum(r.truth_retained for r in scored)),
        unique_rate=ratio(sum(r.unique for r in scored)),
        abstention_rate=ratio(sum(r.abstained for r in scored)),
        mean_candidate_ratio=(sum(ratios) / len(ratios) if ratios else 0.0),
        mean_candidates=ratio(sum(r.n_candidates for r in scored)),
        outcome_counts=dict(sorted(counts.items())),
    )


def run_shadow(cases: list[EvalCase]) -> tuple[list[ShadowResult], ShadowScore]:
    """Run the shadow eval over labeled positive cases and aggregate."""
    results = [shadow_result(c) for c in cases if c.expect_explanation and c.root_cause is not None]
    return results, score_shadow(results)


def load_and_run(cases_dir: str):
    """Convenience for the CLI/script: load a case dir and run the shadow eval."""
    from src.eval.case import load_cases

    return run_shadow(load_cases(Path(cases_dir)))
