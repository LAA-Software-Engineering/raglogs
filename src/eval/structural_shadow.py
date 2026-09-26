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

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.core.rca.outcome import Outcome, resolve
from src.core.rca.partition import partition
from src.core.rca.structural_model import (
    ServiceSignal,
    build_hypotheses,
    build_observables,
    call_edges,
    service_universe,
    structural_signals,
    summarize_metrics,
)
from src.eval.case import EvalCase

__all__ = [
    "ServiceSignal", "summarize_metrics", "call_edges", "build_observables",
    "service_universe", "build_hypotheses", "shadow_result", "ShadowResult", "ClassInfo",
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

    # The same structural-model builder the product path uses (#209 M1): span+metric sig and the
    # incident call graph — one model, not a divergent shadow copy.
    spans = load_spans_jsonl(case.spans_path)
    signals, edges = structural_signals(spans, load_metrics_jsonl(case.metrics_path), case.window_start)
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
