"""Calibrated confidence for the multi-modal RCA prediction (#118 D / #83).

The ranker's top score is *not* P(the top-1 service is the true root cause): a
gradient-boosted `predict_proba` is a per-candidate signal, not a calibrated
probability over the ranked list, and the whole RCA history shows raglogs'
confidence is anti-calibrated (see the project notes / #83). So confidence is a
*separate* estimator trained on held-out top-1 predictions — given features of
the ranked distribution, it predicts P(top-1 correct).

This module is the inference side: :func:`calibration_features` derives the
distribution features from a ranked candidate list, and :func:`calibrated_confidence`
applies a calibrator model. The calibrator is a binary classifier (feature vector
-> P(correct)) and reuses the ranker's non-pickle JSON artifact + pure-Python
evaluator (:mod:`src.core.rca.ranker`), so there is no new model format and no
sklearn/pickle at runtime. Absent a calibrator artifact, callers get ``None`` and
fall back to the existing ordinal confidence — the calibrator is opt-in like the
ranker.

Calibration features (from the ranked candidates):
  ``top_score``            the top candidate's ranker score
  ``margin``               top1 - top2 score gap (0 with a single candidate)
  ``n_candidates``         how many candidate services there were
  ``n_modalities``         modalities backing the top candidate
  ``cross_modal_agreement``  top candidate's modalities / all modalities present
"""
from __future__ import annotations

from typing import Optional

from src.core.rca.candidates import RootCauseCandidate
from src.core.rca.ranker import RcaRanker, load_ranker

# Feature order the calibrator artifact is trained on (its own ``feature_names``
# may use any subset; :func:`calibration_features` always emits all of them).
CALIBRATION_FEATURES: list[str] = [
    "top_score",
    "margin",
    "n_candidates",
    "n_modalities",
    "cross_modal_agreement",
]


def calibration_features(candidates: list[RootCauseCandidate]) -> dict[str, float]:
    """Distribution features of a ranked candidate list (highest first)."""
    if not candidates:
        return {f: 0.0 for f in CALIBRATION_FEATURES}
    top = candidates[0]
    top_score = float(top.score)
    margin = top_score - float(candidates[1].score) if len(candidates) > 1 else 0.0
    present: set[str] = set()
    for c in candidates:
        present.update(c.modalities)
    top_modalities = set(top.modalities)
    agreement = len(top_modalities) / len(present) if present else 0.0
    return {
        "top_score": top_score,
        "margin": margin,
        "n_candidates": float(len(candidates)),
        "n_modalities": float(len(top_modalities)),
        "cross_modal_agreement": agreement,
    }


def calibrated_confidence(
    calibrator: RcaRanker, candidates: list[RootCauseCandidate]
) -> Optional[float]:
    """P(top-1 correct) in [0, 1] for a ranked candidate list, or ``None`` when
    there is nothing to score."""
    if not candidates:
        return None
    feats = calibration_features(candidates)
    x = [feats.get(f, 0.0) for f in calibrator.feature_names]
    return calibrator.score_vector(x)


def load_calibrator(path: Optional[str]) -> Optional[RcaRanker]:
    """Load a calibrator model (same non-pickle JSON artifact as the ranker), or
    ``None`` for graceful fallback to the ordinal confidence."""
    return load_ranker(path)
