"""Failure taxonomy over the **existing** pipeline (#186, Phase H1 — the decision checkpoint).

The #177 epic bet ontology + partitioning (Phases B–E) on the belief that a meaningful share of RCA
misses are *observation-model* / *non-identifiability* failures rather than plain ranking misses. #186
makes that bet falsifiable: bucket the current pipeline's failures **before** extending the machinery,
so the next investment is chosen by data (this is the "measure before extend" checkpoint, #88/#74).

This module is deliberately narrow. It classifies each labeled positive case's *existing* output —
service candidates (`predicted_services`) + the top pick + ground-truth service — into the buckets a
service-level pipeline can determine **without** running the new structural machinery:

- ``CORRECT``   — the top-1 pick is the labeled cause (not a failure).
- ``DETECTION`` — a positive case produced no explanation at all.
- ``COVERAGE``  — the labeled cause was never generated as a candidate.
- ``INFERENCE`` — the labeled cause **was** a candidate but was not ranked top-1.

The finer structural buckets in #177 — ``observability`` / ``ontology`` / ``observation-model`` /
``non-identifiable`` — are refinements of ``COVERAGE`` / ``INFERENCE`` that need the structural shadow
eval (Phase H2) and, for ``D_missing``, per-observable availability ground truth. They are *not*
guessed here; :data:`SCORABLE_AXES` records exactly what each corpus can and cannot score. No
structural inference, no ranking change — just an honest first cut.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src.eval.case import EvalCase
from src.eval.metrics import Prediction


class Bucket(str, Enum):
    """The buckets a service-level pipeline can determine on its own (see module docstring)."""

    CORRECT = "correct"      # top-1 pick == labeled cause — not a failure
    DETECTION = "detection"  # positive case, no explanation produced
    COVERAGE = "coverage"    # labeled cause absent from the candidate set
    INFERENCE = "inference"  # labeled cause in the candidate set but not ranked top-1


def classify_failure(case: EvalCase, pred: Prediction) -> Optional[Bucket]:
    """Bucket one labeled positive case from the existing pipeline's output. Returns ``None`` for
    out-of-scope cases (negative cases, or positive cases with no root-cause label) — those are not
    part of the failure taxonomy. ``CORRECT`` uses the strict **top-1** criterion, so a labeled cause
    present but not top-ranked is an ``INFERENCE`` failure, and one absent entirely is ``COVERAGE``."""
    if not case.expect_explanation or case.root_cause is None:
        return None
    if not pred.produced_explanation:
        return Bucket.DETECTION
    truth = case.root_cause.service
    if pred.root_cause_service == truth:
        return Bucket.CORRECT
    if truth in pred.predicted_services:
        return Bucket.INFERENCE
    return Bucket.COVERAGE


@dataclass
class TaxonomyReport:
    """Distribution of the failure taxonomy over the scored (labeled positive) cases."""

    n_scored: int = 0
    counts: dict[str, int] = field(default_factory=dict)      # bucket value -> count
    case_ids: dict[str, list[str]] = field(default_factory=dict)  # bucket value -> case ids

    def share(self, bucket: Bucket) -> Optional[float]:
        """The fraction of scored cases in ``bucket`` (``None`` when nothing was scored)."""
        if self.n_scored == 0:
            return None
        return self.counts.get(bucket.value, 0) / self.n_scored

    @property
    def failure_share(self) -> Optional[float]:
        """Fraction of scored cases that are any failure (not ``CORRECT``)."""
        if self.n_scored == 0:
            return None
        failures = self.n_scored - self.counts.get(Bucket.CORRECT.value, 0)
        return failures / self.n_scored


def build_taxonomy(pairs: list[tuple[EvalCase, Prediction]]) -> TaxonomyReport:
    """Aggregate the failure taxonomy over ``(case, prediction)`` pairs (one arm). Pure — no DB."""
    counts: Counter[str] = Counter()
    case_ids: dict[str, list[str]] = defaultdict(list)
    n_scored = 0
    for case, pred in pairs:
        bucket = classify_failure(case, pred)
        if bucket is None:
            continue
        n_scored += 1
        counts[bucket.value] += 1
        case_ids[bucket.value].append(case.id)
    return TaxonomyReport(
        n_scored=n_scored,
        counts=dict(counts),
        case_ids={k: sorted(v) for k, v in case_ids.items()},
    )


# The corpus × scorable-axis matrix #186 asks for: what each corpus can honestly score. "existing"
# axes are determinable now (this module); "structural" ones need the Phase H2 shadow eval; "avail-gt"
# ones additionally need per-observable availability labels (only the synthetic trace corpus #170 has
# them — see project_otel_frozen_external_validation for why external transfer is reported separately).
SCORABLE_AXES: dict[str, str] = {
    "detection": "existing",
    "coverage": "existing",
    "inference": "existing",
    "structural_outcome": "structural",   # IDENTIFIED/NON_IDENTIFIABLE/IRREDUCIBLE/UNCERTAIN/NO_COMPATIBLE
    "observability": "structural",
    "ontology": "structural",             # also needs non-service (edge/resource/...) ground truth
    "observation_model": "structural",
    "non_identifiable": "structural",
    "d_missing_correctness": "avail-gt",  # synthetic trace corpus #170 only
    "external_transfer": "existing",      # reported separately, never tuned to (OTel lesson)
}
