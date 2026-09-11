"""Abstention gate (#79) — "is there enough evidence of an incident to diagnose?"

Separate from the root-cause ranker ("which service?", #118) and the confidence
calibrator ("how likely is that prediction right?", #83). The gate answers a prior
question: on a *healthy* window raglogs should say **insufficient evidence** rather
than manufacture an incident (the frozen OTel run abstained on 0/2 healthy windows).

Design contract (see ``docs/design-abstention.md``):

1. **Stabilize first, saturate second.** Each modality's raw signal becomes a
   stabilized, non-negative effect size, *then* a fixed saturating transform maps it
   to ``[0, 1]``. Bounding a raw ratio is not enough — a near-zero baseline would
   otherwise map ``4.7e7 -> 0.999999`` and the score would still track logging
   volume rather than incident strength.
2. **All parameters frozen from development data.** The scales ``tau`` *and* the
   abstention threshold are selected on RCAEval (nested, leave-one-system-out) and
   frozen; nothing here is adapted from local traffic at inference.
3. **Missing modality is absent, not zero evidence.** Fusion is ``max`` over the
   *available* arms only; with no available modality the gate abstains (no evidence
   to diagnose from).

This module is pure (no DB, no settings import) so it is unit-testable and reused
by both the offline calibration harness and the pipeline. Callers pass the fixed
``tau`` scales and threshold (from settings); the transforms themselves are fixed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def saturate(x: float, tau: float) -> float:
    """Map a non-negative effect size ``x`` to ``[0, 1)`` via ``1 - exp(-x/tau)``.

    ``x`` is clamped at 0 (a below-baseline window is "no anomaly", never negative
    evidence). ``tau > 0`` is the fixed scale; a non-positive ``tau`` degenerates to
    "any positive anomaly saturates" (returns 1.0 for x>0, 0.0 for x==0)."""
    x = max(0.0, x)
    if tau <= 0:
        return 1.0 if x > 0 else 0.0
    return 1.0 - math.exp(-x / tau)


def log_rate_anomaly(incident_rate: float, baseline_rate: float, tau: float) -> float:
    """Log arm: stabilized log-rate effect size, then saturate.

    ``max(0, log(1+incident_rate) - log(1+baseline_rate))`` — the ``log1p`` on each
    rate keeps a near-zero baseline from producing a pathological input, and the
    clamp drops below-baseline windows to 0."""
    x = math.log1p(max(0.0, incident_rate)) - math.log1p(max(0.0, baseline_rate))
    return saturate(x, tau)


def ratio_anomaly(ratio: float, tau: float) -> float:
    """Trace arm: an incident/baseline ratio (>= 0) → ``max(0, log(ratio))`` then
    saturate. ``log(ratio)`` is the symmetric, dimensionless elevation; clamped so
    only elevation (ratio > 1) counts."""
    x = math.log(ratio) if ratio > 0 else 0.0
    return saturate(x, tau)


def magnitude_anomaly(change: float, tau: float) -> float:
    """Metric arm: an already-non-negative relative change magnitude → saturate.
    (Two-sided metrics should pass ``abs(deviation)``.)"""
    return saturate(max(0.0, change), tau)


@dataclass(frozen=True)
class WindowAnomaly:
    """Fused window-anomaly score in ``[0, 1]`` and which modalities contributed."""

    score: float
    available: tuple[str, ...]

    @property
    def has_evidence(self) -> bool:
        return bool(self.available)


def window_anomaly(
    *,
    logs: float | None = None,
    traces: float | None = None,
    metrics: float | None = None,
) -> WindowAnomaly:
    """Fuse the per-modality saturated anomalies (each already in ``[0, 1]``) by
    ``max`` over the **available** arms. ``None`` means the modality is absent (no
    data) and is dropped from the fusion — it must not read as ``0`` ("all clear").
    With no available modality the score is ``0.0`` and ``available`` is empty, so
    :func:`should_abstain` will abstain."""
    arms = {
        name: value
        for name, value in (("logs", logs), ("traces", traces), ("metrics", metrics))
        if value is not None
    }
    if not arms:
        return WindowAnomaly(0.0, ())
    return WindowAnomaly(max(arms.values()), tuple(arms))


def should_abstain(anomaly: WindowAnomaly, threshold: float) -> bool:
    """Abstain when there is no available modality, or the fused window-anomaly
    score is below the frozen ``threshold``. The threshold is chosen on development
    data by a recall-constrained objective (maximise healthy-window abstention
    subject to a high incident-recall floor), so it is deliberately lenient:
    suppressing a real incident is the expensive mistake."""
    if not anomaly.has_evidence:
        return True
    return anomaly.score < threshold
