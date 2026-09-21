"""Structural shadow evaluation — the minimal A–E bridge (#186, Phase H2 of the #177 epic).

Phase H1 (#201) bucketed the *existing* pipeline's failures. This is the follow-up the decision
checkpoint on #177 calls for: run the A–E structural core (#178–#183) on **real** eval telemetry,
**offline / in shadow**, and score the five outcomes against ground truth alongside the baseline —
does the structural machinery retain (and sometimes uniquely pin) the causes the log-cluster pipeline
misses as coverage/inference? It changes **nothing** in the product explain path.

The causal model here is a deliberately **minimal v1**, its only job to exercise A–E on real traces:

- **Observables** — one per service that has metrics: ``sig:{service}`` = ``PRESENT`` when the service
  is anomalous this incident (error-rate present *or* latency ≥2× baseline, discretized via the
  Phase D policy), else ``ABSENT``; ``OBSERVED`` because we measured it. A service with no metrics is
  simply absent (its coordinate stays UNKNOWN), which is exactly how A–E wants missing telemetry.
- **Hypotheses** — one ``process`` hypothesis per candidate service. Candidates are the anomalous
  services **plus their callees** (propagation-aware, so a *silent* downstream root is still
  generated — the coverage gap H1 measured). Each hypothesis ``R`` predicts ``sig:{R}=PRESENT`` and is
  **hard-contradicted** by ``sig:{callee}=PRESENT`` for any callee of ``R`` — a failing callee means
  ``R`` is not the root, the callee is deeper.

What that yields, and why it is the honest structural answer:

- ``callee_fail`` (the root emits its own error): the caller-as-root hypotheses are hard-eliminated up
  the path, leaving the deepest failing node → ``IDENTIFIED`` (unique). The existing pipeline scored
  these as ``INFERENCE`` (the loud caller outranks the root).
- ``symptom_only`` (the root is silent): nothing eliminates the caller, and the silent root's own
  coordinate can't separate it from the symptom service → ``UNCERTAIN`` with the truth **retained**
  (``struct_ok``, not unique). The existing pipeline scored these as ``COVERAGE`` (the root was never
  generated at all).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.core.rca.expectations import Contradiction, ExpectedObservation, ObservationModel
from src.core.rca.hypothesis import Kind, from_observation_model
from src.core.rca.observable import Observable, State, observed
from src.core.rca.outcome import Outcome, resolve
from src.core.rca.partition import discretize_rate, discretize_ratio, partition
from src.eval.case import EvalCase


@dataclass(frozen=True)
class ServiceSignal:
    """A service's incident-vs-baseline summary and whether it reads as anomalous."""

    service: str
    error_rate: float      # mean incident error rate
    latency_ratio: float   # mean incident latency / mean baseline latency (1.0 if no baseline)
    anomalous: bool


def summarize_metrics(samples: list, window_start: datetime) -> dict[str, ServiceSignal]:
    """Summarize ``ParsedMetricSample`` records into a per-service signal. Samples at/after
    ``window_start`` are the incident; earlier ones are the baseline. Anomalous = error rate present
    (Phase D ``discretize_rate``) or latency ≥2× baseline (``discretize_ratio``)."""
    inc: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    base: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for s in samples:
        if s.service is None or s.value is None or s.ts is None:
            continue
        bucket = inc if s.ts >= window_start else base
        bucket[s.service][s.metric].append(float(s.value))

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    signals: dict[str, ServiceSignal] = {}
    for svc in sorted(set(inc) | set(base)):
        err = mean(inc[svc].get("error_rate", []))
        inc_lat = mean(inc[svc].get("latency_ms", []))
        base_lat = mean(base[svc].get("latency_ms", []))
        ratio = inc_lat / base_lat if base_lat > 0 else 1.0
        anomalous = (discretize_rate(min(err, 1.0)) == State.PRESENT
                     or discretize_ratio(max(ratio, 0.0)) == State.HIGH)
        signals[svc] = ServiceSignal(svc, err, ratio, anomalous)
    return signals


def call_edges(spans: list) -> set[tuple[str, str]]:
    """Distinct (caller_service, callee_service) edges from ``ParsedSpan`` records via parent links."""
    service_of = {sp.span_id: sp.service for sp in spans if sp.span_id and sp.service}
    edges: set[tuple[str, str]] = set()
    for sp in spans:
        caller = service_of.get(sp.parent_span_id) if sp.parent_span_id else None
        if caller and sp.service and caller != sp.service:
            edges.add((caller, sp.service))
    return edges


def build_observables(signals: dict[str, ServiceSignal]) -> list[Observable]:
    """One ``sig:{service}`` observable per measured service (OBSERVED PRESENT/ABSENT)."""
    return [
        observed(f"sig:{svc}", State.PRESENT if sig.anomalous else State.ABSENT)
        for svc, sig in sorted(signals.items())
    ]


def _callees(service: str, edges: set[tuple[str, str]]) -> set[str]:
    return {callee for caller, callee in edges if caller == service}


def build_hypotheses(signals: dict[str, ServiceSignal], edges: set[tuple[str, str]]):
    """Candidate ``process`` hypotheses. Each anomalous service is a candidate; a service's **silent**
    callees are added as candidates only when *no* callee is already anomalous — i.e. the fault is not
    yet explained by a visible downstream failure, so a silent callee could be the (unobserved) root.
    That is what lets a silent root be generated (the coverage gap) without a healthy sibling callee
    polluting a case whose root does emit its own error. Each candidate predicts its own ``sig``
    present and is hard-contradicted by a failing callee (a failing callee means it is not the root)."""
    anomalous = {s for s, sig in signals.items() if sig.anomalous}
    candidates = set(anomalous)
    for s in anomalous:
        callee_set = _callees(s, edges)
        if not any(c in anomalous for c in callee_set):  # fault unexplained by a visible callee
            candidates |= callee_set
    hypotheses = []
    for svc in sorted(candidates):
        model = ObservationModel(
            expected=(ExpectedObservation(f"sig:{svc}", State.PRESENT),),
            contradictions=tuple(
                Contradiction(f"sig:{c}", frozenset({State.PRESENT})) for c in sorted(_callees(svc, edges))
            ),
        )
        hypotheses.append(from_observation_model(f"process:{svc}", Kind.PROCESS, svc, model))
    return hypotheses


@dataclass(frozen=True)
class ShadowResult:
    """One case's structural shadow outcome, scored against the labeled root cause."""

    case_id: str
    truth: str
    outcome: str            # Outcome value, or "no_candidates" when nothing was generated
    localizations: tuple[str, ...]
    struct_ok: bool         # the labeled cause is retained in a surviving class
    unique: bool            # IDENTIFIED and the sole localization is the labeled cause
    abstained: bool         # NO_COMPATIBLE_HYPOTHESIS (everything hard-eliminated)


def shadow_result(case: EvalCase) -> ShadowResult:
    """Build observables + hypotheses from the case's own telemetry, partition, resolve, and score.
    Requires a labeled positive case with metric + span sidecars."""
    from src.eval.rcaeval import load_metrics_jsonl, load_spans_jsonl

    truth = case.root_cause.service if case.root_cause else ""
    if case.metrics_path is None or case.spans_path is None:
        return ShadowResult(case.id, truth, "no_telemetry", (), False, False, False)

    signals = summarize_metrics(load_metrics_jsonl(case.metrics_path), case.window_start)
    edges = call_edges(load_spans_jsonl(case.spans_path))
    hypotheses = build_hypotheses(signals, edges)
    if not hypotheses:
        return ShadowResult(case.id, truth, "no_candidates", (), False, False, False)

    result = resolve(partition(hypotheses, build_observables(signals)))
    localizations = result.localization
    struct_ok = truth in localizations
    unique = result.outcome is Outcome.IDENTIFIED and localizations == (truth,)
    return ShadowResult(
        case_id=case.id, truth=truth, outcome=result.outcome.value,
        localizations=localizations, struct_ok=struct_ok, unique=unique,
        abstained=result.outcome is Outcome.NO_COMPATIBLE_HYPOTHESIS,
    )


@dataclass
class ShadowScore:
    """Aggregate structural shadow metrics over a corpus (labeled positive cases only)."""

    n: int
    struct_ok_rate: float
    unique_rate: float
    abstention_rate: float
    outcome_counts: dict[str, int]


def score_shadow(results: list[ShadowResult]) -> ShadowScore:
    scored = [r for r in results if r.truth]
    n = len(scored)
    counts: dict[str, int] = defaultdict(int)
    for r in scored:
        counts[r.outcome] += 1
    ratio = lambda k: (k / n if n else 0.0)  # noqa: E731
    return ShadowScore(
        n=n,
        struct_ok_rate=ratio(sum(r.struct_ok for r in scored)),
        unique_rate=ratio(sum(r.unique for r in scored)),
        abstention_rate=ratio(sum(r.abstained for r in scored)),
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
