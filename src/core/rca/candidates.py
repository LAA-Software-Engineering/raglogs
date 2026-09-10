"""Modality-neutral root-cause candidates (#118 C1c).

The pipeline historically equated "root cause" with the primary *log* cluster's
dominant service. That is wrong for the propagation and resource/network faults
the multi-modal work targets, where the signal lives in traces or metrics and
there may be no distinctive log cluster at all (ChatGPT design review, #124).

:class:`RootCauseCandidate` decouples the root-cause *identity* (a service) from
any single modality: a candidate carries a score plus the evidence that supports
it, drawn from whichever of logs / traces / metrics actually fired. This is the
structure the ranker (C2) ranks and the confidence calibrator (D) consumes.

C1c introduces the type and the evidence assembly only. :func:`build_candidates`
takes an injectable ``scorer`` so C2 can drop in the learned ranker; the default
scorer reproduces today's behaviour (the trivial-baseline selection key, the
largest single ``(service, fingerprint)`` error group) so nothing downstream
changes until the ranker lands. Nothing in the explain path calls this yet.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from src.core.rca.features import FeatureTable, ServiceFeatures


@dataclass
class ModalityEvidence:
    """One modality's contribution to a candidate — a human-readable ``detail``
    plus the raw ``signals`` behind it (for the API / confidence features)."""

    modality: str  # "logs" | "traces" | "metrics"
    detail: str
    signals: dict[str, float] = field(default_factory=dict)


@dataclass
class RootCauseCandidate:
    service: str
    score: float
    features: ServiceFeatures
    evidence: list[ModalityEvidence] = field(default_factory=list)

    @property
    def modalities(self) -> list[str]:
        return [e.modality for e in self.evidence]

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "score": self.score,
            "modalities": self.modalities,
            "evidence": [
                {"modality": e.modality, "detail": e.detail, "signals": e.signals}
                for e in self.evidence
            ],
            "features": self.features.as_dict(),
        }


def default_scorer(sf: ServiceFeatures) -> float:
    """Placeholder score until the learned ranker (C2) replaces it: the largest
    single ``(service, fingerprint)`` error group — the trivial-baseline
    selection key the current pipeline already uses. Keeps ``build_candidates``
    behaviour-neutral by default."""
    return float(sf.log_grp)


def _evidence_for(sf: ServiceFeatures) -> list[ModalityEvidence]:
    ev: list[ModalityEvidence] = []
    if sf.log_err > 0:
        ev.append(
            ModalityEvidence(
                modality="logs",
                detail=(
                    f"{sf.log_err} error lines "
                    f"(top fingerprint group {sf.log_grp}, {sf.log_stack} stack-trace lines)"
                ),
                signals={
                    "log_err": float(sf.log_err),
                    "log_grp": float(sf.log_grp),
                    "log_stack": float(sf.log_stack),
                },
            )
        )
    if sf.tr_rate > 0 or sf.tr_dur > 0:
        ev.append(
            ModalityEvidence(
                modality="traces",
                detail=f"span-rate {sf.tr_rate:.1f}x baseline, p95 latency {sf.tr_dur:.1f}x",
                signals={"tr_rate": sf.tr_rate, "tr_dur": sf.tr_dur},
            )
        )
    if sf.met_anom > 0:
        ev.append(
            ModalityEvidence(
                modality="metrics",
                detail=f"metric change {sf.met_anom:.1f}x baseline",
                signals={"met_anom": sf.met_anom},
            )
        )
    return ev


def build_candidates(
    table: FeatureTable,
    scorer: Callable[[ServiceFeatures], float] = default_scorer,
    exclude: frozenset[str] = frozenset(),
) -> list[RootCauseCandidate]:
    """Turn a :class:`FeatureTable` into scored, evidence-backed candidates,
    highest score first (ties broken by service name for determinism).

    ``exclude`` drops services that are never a root cause — a deployment-specific
    denylist for traffic generators / infra sidecars (e.g. a load generator, a flag
    daemon, an ingress proxy) that carry heavy telemetry but can't be the fault.
    Empty by default so core stays deployment-agnostic (the names live in config,
    not here — #81)."""
    candidates = [
        RootCauseCandidate(
            service=sf.service,
            score=float(scorer(sf)),
            features=sf,
            evidence=_evidence_for(sf),
        )
        for sf in table.services
        if sf.service not in exclude
    ]
    candidates.sort(key=lambda c: (-c.score, c.service))
    return candidates
