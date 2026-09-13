"""Trace-localization benchmark: rich causal labels + a causal-localization scorer.

RCAEval can validate the log-dependent reranker but **cannot** answer the question the
trace work now cares about — can trace topology / ERROR spans tell an *upstream cause*
from a *downstream propagated symptom*? — because its cases are 100% oracle (the truth
is always already a candidate) and carry essentially no ERROR-status spans (see
``docs/eval-trace-propagation.md``). This module defines a small, deliberately-adversarial
benchmark whose labels are rich enough to score *causal localization*, not just top-1.

A case's ground truth (the ``trace_localization`` block of its ``case.yaml``) carries:

  ``root_cause``          the injected culprit service
  ``first_failing``       the service whose telemetry degrades first (usually the cause)
  ``propagation_path``    ordered cause -> ... -> symptom services the fault travels
  ``symptom_services``    services that only *report* the failure (loud but not the cause)
  ``edges``               caller -> callee dependency edges (the call graph)
  ``fault_type``          caller_fail | callee_fail | symptom_only | latency_only

The point of the ``symptom_services`` label is the adversarial test: the trivial
"loudest-error / busiest-caller" baseline ranks a symptom first, so a method that scores
well here is doing *causal* work, not correlating telemetry volume. The scorer therefore
reports, besides top-1/top-3, a **cause-above-symptom** rate: of the cases with a distinct
loud symptom, how often the method ranks the true cause above every symptom service.

Pure over plain records so it is unit-testable without a DB.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

FAULT_TYPES = frozenset({"caller_fail", "callee_fail", "symptom_only", "latency_only"})


@dataclass
class TraceLocCase:
    """Ground-truth causal labels for one trace-localization case."""

    id: str
    root_cause: str
    fault_type: str
    first_failing: str
    propagation_path: list[str] = field(default_factory=list)
    symptom_services: list[str] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)

    @property
    def has_distinct_symptom(self) -> bool:
        """True when a symptom service (not the cause) is labelled — the cases where
        cause-above-symptom is a meaningful, adversarial question."""
        return any(s != self.root_cause for s in self.symptom_services)


def load_trace_loc_labels(case_yaml: Path) -> TraceLocCase | None:
    """Read the ``trace_localization`` block from a case's ``case.yaml``; ``None`` when
    the case has no such block (it is then not a trace-localization case)."""
    import yaml

    raw = yaml.safe_load(Path(case_yaml).read_text()) or {}
    tl = raw.get("trace_localization")
    if not tl:
        return None
    if not tl.get("root_cause"):
        raise ValueError(f"{case_yaml}: trace_localization.root_cause is required")
    ft = str(tl.get("fault_type", ""))
    if ft not in FAULT_TYPES:
        raise ValueError(f"{case_yaml}: trace_localization.fault_type must be one of {sorted(FAULT_TYPES)}")
    return TraceLocCase(
        id=str(raw.get("id") or Path(case_yaml).parent.name),
        root_cause=str(tl["root_cause"]),
        fault_type=ft,
        first_failing=str(tl.get("first_failing") or tl["root_cause"]),
        propagation_path=[str(s) for s in (tl.get("propagation_path") or [])],
        symptom_services=[str(s) for s in (tl.get("symptom_services") or [])],
        edges=[(str(a), str(b)) for a, b in (tl.get("edges") or [])],
    )


@dataclass
class LocResult:
    """One method's prediction for a case: its ranked candidate services (best first),
    and optionally the service it named as first-failing."""

    ranked_services: list[str]
    first_failing_pred: str | None = None

    def top1_correct(self, case: TraceLocCase) -> bool:
        return bool(self.ranked_services) and self.ranked_services[0] == case.root_cause

    def top3_correct(self, case: TraceLocCase) -> bool:
        return case.root_cause in self.ranked_services[:3]

    def cause_above_symptom(self, case: TraceLocCase) -> bool | None:
        """Is the true cause ranked above *every* labelled symptom service? ``None`` only
        when the case has no distinct symptom (the question doesn't apply). An unranked
        cause returns ``False`` — a failure, not an exclusion: on a ``symptom_only`` case
        the cause scores ~0 and isn't a candidate, and that must count against the metric
        (it is exactly why the reported ``symptom_only`` rate is 0%), never be dropped from
        the denominator."""
        if not case.has_distinct_symptom:
            return None
        order = {s: i for i, s in enumerate(self.ranked_services)}
        if case.root_cause not in order:
            return False
        cause_rank = order[case.root_cause]
        symptom_ranks = [order[s] for s in case.symptom_services if s != case.root_cause and s in order]
        # A symptom that isn't even a candidate can't outrank the cause.
        return all(cause_rank < r for r in symptom_ranks)

    def first_failing_correct(self, case: TraceLocCase) -> bool | None:
        if self.first_failing_pred is None:
            return None
        return self.first_failing_pred == case.first_failing


def _rate(flags: list[bool]) -> float | None:
    real = [f for f in flags if f is not None]
    return (sum(1 for f in real if f) / len(real)) if real else None


def score_localization(pairs: list[tuple[TraceLocCase, LocResult]]) -> dict:
    """Aggregate causal-localization metrics over ``(case, result)`` pairs.

    Besides top-1/top-3, the headline is **cause_above_symptom** — the adversarial metric
    the trivial loudest-error baseline is designed to fail — plus first-failing accuracy
    and per-fault-type top-1, so a wash or a win is legible by fault family.
    """
    top1 = _rate([r.top1_correct(c) for c, r in pairs])
    top3 = _rate([r.top3_correct(c) for c, r in pairs])
    cas = _rate([r.cause_above_symptom(c) for c, r in pairs])
    ff = _rate([r.first_failing_correct(c) for c, r in pairs])

    by_fault: dict[str, dict] = {}
    for ft in sorted({c.fault_type for c, _ in pairs}):
        grp = [(c, r) for c, r in pairs if c.fault_type == ft]
        by_fault[ft] = {
            "n": len(grp),
            "top1": _rate([r.top1_correct(c) for c, r in grp]),
            "cause_above_symptom": _rate([r.cause_above_symptom(c) for c, r in grp]),
        }
    return {
        "n": len(pairs),
        "top1": top1,
        "top3": top3,
        "cause_above_symptom": cas,
        "first_failing": ff,
        "per_fault_type": by_fault,
    }
