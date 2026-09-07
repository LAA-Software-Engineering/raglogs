"""Metric families scored per case, plus aggregation and baseline lift.

All scoring here is pure: it takes a :class:`Prediction` (the normalized output
of either arm) and an :class:`~src.eval.case.EvalCase`, so it is unit-testable
without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from src.eval.case import EvalCase

# The labeled trigger timestamp is "hit" if the top candidate lands within this.
DEFAULT_TRIGGER_TOLERANCE = timedelta(minutes=5)


@dataclass
class Prediction:
    """Normalized output of one arm (raglogs or baseline) for one case."""

    produced_explanation: bool
    root_cause_service: Optional[str] = None
    predicted_services: list[str] = field(default_factory=list)
    top_trigger_timestamp: Optional[datetime] = None
    returned_any_trigger: bool = False
    confidence: str = "low"


def root_cause_hit(case: EvalCase, pred: Prediction) -> Optional[bool]:
    """Does the primary cluster's services contain the labeled service?

    ``None`` when the case has no root-cause label (negative cases).
    """
    if case.root_cause is None:
        return None
    return case.root_cause.service in pred.predicted_services


def trigger_hit(
    case: EvalCase,
    pred: Prediction,
    tolerance: timedelta = DEFAULT_TRIGGER_TOLERANCE,
) -> Optional[bool]:
    """Is the top trigger candidate within tolerance of the labeled trigger?

    ``None`` when the case has no labeled trigger timestamp.
    """
    if case.trigger is None or case.trigger.timestamp is None:
        return None
    if pred.top_trigger_timestamp is None:
        return False
    return abs(pred.top_trigger_timestamp - case.trigger.timestamp) <= tolerance


def negative_correct(case: EvalCase, pred: Prediction) -> Optional[bool]:
    """On a negative case, did the arm correctly abstain (no explanation)?

    ``None`` for positive cases (``expect_explanation: true``).
    """
    if case.expect_explanation:
        return None
    return not pred.produced_explanation


# ── Aggregation ───────────────────────────────────────────────────────────────


@dataclass
class ArmScore:
    """Aggregate metric families for one arm over a set of cases."""

    root_cause_accuracy: Optional[float] = None
    trigger_accuracy: Optional[float] = None
    any_trigger_rate: Optional[float] = None
    negative_precision: Optional[float] = None
    # confidence bucket -> (accuracy, n)
    calibration: dict[str, tuple[float, int]] = field(default_factory=dict)
    n_positive: int = 0
    n_negative: int = 0
    n_with_trigger: int = 0


def _ratio(hits: int, total: int) -> Optional[float]:
    return None if total == 0 else hits / total


def _case_correct(case: EvalCase, pred: Prediction) -> Optional[bool]:
    """Single correctness signal used for calibration bucketing.

    Positive cases: correct == root-cause service hit. Negative cases: correct
    == abstained. ``None`` when neither applies.
    """
    if case.expect_explanation:
        return root_cause_hit(case, pred)
    return negative_correct(case, pred)


def score_arm(
    pairs: list[tuple[EvalCase, Prediction]],
    tolerance: timedelta = DEFAULT_TRIGGER_TOLERANCE,
) -> ArmScore:
    """Aggregate one arm's predictions across all cases into metric families."""
    rc_hits = rc_total = 0
    trig_hits = trig_total = 0
    any_trig_hits = any_trig_total = 0
    neg_hits = neg_total = 0
    n_positive = n_negative = 0
    buckets: dict[str, list[bool]] = {}

    for case, pred in pairs:
        if case.expect_explanation:
            n_positive += 1
        else:
            n_negative += 1

        rc = root_cause_hit(case, pred)
        if rc is not None:
            rc_total += 1
            rc_hits += int(rc)

        th = trigger_hit(case, pred, tolerance)
        if th is not None:
            trig_total += 1
            trig_hits += int(th)
            any_trig_total += 1
            any_trig_hits += int(pred.returned_any_trigger)

        nc = negative_correct(case, pred)
        if nc is not None:
            neg_total += 1
            neg_hits += int(nc)

        correct = _case_correct(case, pred)
        if correct is not None:
            buckets.setdefault(pred.confidence, []).append(correct)

    calibration = {
        conf: (sum(vals) / len(vals), len(vals)) for conf, vals in sorted(buckets.items())
    }

    return ArmScore(
        root_cause_accuracy=_ratio(rc_hits, rc_total),
        trigger_accuracy=_ratio(trig_hits, trig_total),
        any_trigger_rate=_ratio(any_trig_hits, any_trig_total),
        negative_precision=_ratio(neg_hits, neg_total),
        calibration=calibration,
        n_positive=n_positive,
        n_negative=n_negative,
        n_with_trigger=trig_total,
    )
