"""Unit tests for calibrated confidence wired into the label (#83). No DB.

When a ranker + calibrator produce P(top-1 root-cause correct), the confidence
label is bucketed from that probability and the API score is the probability
(calibrated=True). Without one, the legacy ordinal path is unchanged."""
from datetime import datetime, timezone

import pytest

from src.api.schemas.v1 import _confidence_for_result, explain_from_result
from src.core.explain.confidence import label_from_calibrated_probability
from src.core.explain.summarizer import ExplainResult

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _result(**kw) -> ExplainResult:
    base = dict(
        window_start=T0, window_end=T0, summary_text="ok", confidence="medium",
        evidence_items=[], services_affected=[],
    )
    base.update(kw)
    return ExplainResult(**base)


class TestLabelFromProbability:
    @pytest.mark.parametrize("p,label", [
        (0.95, "high"), (0.80, "high"),
        (0.70, "medium-high"), (0.65, "medium-high"),
        (0.62, "medium"), (0.45, "medium"),
        (0.31, "low"), (0.0, "low"),
    ])
    def test_bands(self, p, label):
        assert label_from_calibrated_probability(p) == label


class TestApiConfidence:
    def test_calibrated_score_is_the_probability(self):
        # summarizer sets result.confidence to the bucketed label already; the API
        # carries the probability as score + calibrated=True.
        res = _result(confidence="medium-high", predicted_root_cause_confidence=0.78)
        resp = explain_from_result(res, no_llm=True, cached=False)
        assert resp.confidence.calibrated is True
        assert resp.confidence.score == pytest.approx(0.78)
        assert resp.confidence.label == "medium-high"

    def test_legacy_path_is_ordinal_and_not_calibrated(self):
        res = _result(confidence="high", predicted_root_cause_confidence=None)
        resp = explain_from_result(res, no_llm=True, cached=False)
        assert resp.confidence.calibrated is False
        assert resp.confidence.label == "high"
        # ordinal score derived from the label, not a probability
        assert 0.0 <= resp.confidence.score <= 1.0

    def test_confidence_for_result_helper(self):
        assert _confidence_for_result(_result(predicted_root_cause_confidence=0.9)).calibrated
        assert not _confidence_for_result(_result()).calibrated
