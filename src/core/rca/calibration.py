"""Calibrated confidence for the multi-modal RCA prediction (#118 D / #83).

The ranker's top score is *not* P(the top-1 service is the true root cause): a
gradient-boosted `predict_proba` is a per-candidate signal, not a calibrated
probability over the ranked list, and raglogs' confidence is measurably
anti-calibrated (#83). So confidence is a *separate* estimator over the ranked
distribution, trained on held-out top-1 predictions, that predicts P(top-1
correct).

**Model choice is measured, not assumed** (see ``docs/eval-rca-calibrator.md``).
Nested leave-one-system-out on RE2+RE3 shows a flexible calibrator (gradient
boosting over all the distribution features) *overfits* out-of-system and makes
RE3 calibration worse. A low-capacity **Platt scaling** — a 1-D logistic on the
ranker's ``top_score`` — roughly halves ECE on both corpora and generalises. So
the calibrator here is Platt scaling: ``P = sigmoid(a * top_score + b)``.

:func:`calibration_features` still exposes the full distribution (``top_score``,
``margin``, …) for analysis and future models, but the shipped calibrator uses
only ``top_score``. The artifact is a tiny non-pickle JSON ``{a, b}`` and inference
is pure-Python — no sklearn/pickle at runtime. Absent an artifact, callers get
``None`` and fall back to the ordinal confidence (opt-in like the ranker).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.core.rca.candidates import RootCauseCandidate

CALIBRATOR_VERSION = 1

# Distribution features of a ranked candidate list (for analysis; the shipped
# Platt calibrator keys on ``top_score`` only — the others overfit, see the doc).
CALIBRATION_FEATURES: list[str] = [
    "top_score",
    "margin",
    "n_candidates",
    "n_modalities",
    "cross_modal_agreement",
]


def calibration_features(candidates: list[RootCauseCandidate]) -> dict[str, float]:
    """Distribution features of a ranked candidate list (highest score first)."""
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


@dataclass
class PlattCalibrator:
    """Platt scaling over a single ranked-distribution feature (default
    ``top_score``): ``P(top-1 correct) = sigmoid(a * feature + b)``."""

    a: float
    b: float
    feature: str = "top_score"

    def probability(self, feature_value: float) -> float:
        return 1.0 / (1.0 + math.exp(-(self.a * feature_value + self.b)))

    def confidence(self, candidates: list[RootCauseCandidate]) -> Optional[float]:
        if not candidates:
            return None
        return self.probability(calibration_features(candidates)[self.feature])

    def to_dict(self) -> dict:
        return {
            "version": CALIBRATOR_VERSION,
            "type": "platt",
            "feature": self.feature,
            "a": self.a,
            "b": self.b,
        }


def calibrator_from_dict(data: dict) -> PlattCalibrator:
    if data.get("version") != CALIBRATOR_VERSION or data.get("type") != "platt":
        raise ValueError(f"unsupported calibrator artifact: {data.get('type')!r} v{data.get('version')!r}")
    feature = str(data.get("feature", "top_score"))
    if feature not in CALIBRATION_FEATURES:
        raise ValueError(f"unknown calibrator feature: {feature!r}")
    return PlattCalibrator(a=float(data["a"]), b=float(data["b"]), feature=feature)


def calibrated_confidence(
    calibrator: PlattCalibrator, candidates: list[RootCauseCandidate]
) -> Optional[float]:
    """P(top-1 correct) in [0, 1] for a ranked candidate list, or ``None`` when
    there is nothing to score."""
    return calibrator.confidence(candidates)


def load_calibrator(path: Optional[str]) -> Optional[PlattCalibrator]:
    """Load the calibrator artifact, or ``None`` for graceful fallback to the
    ordinal confidence (absent / missing / unparseable path)."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return calibrator_from_dict(json.loads(p.read_text()))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
