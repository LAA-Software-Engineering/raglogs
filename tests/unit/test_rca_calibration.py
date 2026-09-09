"""Unit tests for RCA confidence calibration (#118 D / #83). No DB.

The calibrator is Platt scaling on the ranker top_score (model choice validated
in docs/eval-rca-calibrator.md)."""
import json

import pytest

from src.core.rca.calibration import (
    CALIBRATION_FEATURES,
    PlattCalibrator,
    calibrated_confidence,
    calibration_features,
    calibrator_from_dict,
    load_calibrator,
)
from src.core.rca.candidates import ModalityEvidence, RootCauseCandidate
from src.core.rca.features import ServiceFeatures


def _cand(service, score, modalities):
    ev = [ModalityEvidence(modality=m, detail=m) for m in modalities]
    return RootCauseCandidate(service=service, score=score, features=ServiceFeatures(service=service), evidence=ev)


class TestCalibrationFeatures:
    def test_margin_and_counts(self):
        cands = [
            _cand("a", 0.8, ["logs", "metrics"]),
            _cand("b", 0.5, ["traces"]),
            _cand("c", 0.1, ["logs"]),
        ]
        f = calibration_features(cands)
        assert f["top_score"] == pytest.approx(0.8)
        assert f["margin"] == pytest.approx(0.3)
        assert f["n_candidates"] == 3.0
        assert f["n_modalities"] == 2.0
        assert f["cross_modal_agreement"] == pytest.approx(2 / 3)

    def test_single_candidate_zero_margin(self):
        f = calibration_features([_cand("a", 0.9, ["metrics"])])
        assert f["margin"] == 0.0
        assert f["cross_modal_agreement"] == pytest.approx(1.0)

    def test_empty_is_all_zero(self):
        assert calibration_features([]) == {k: 0.0 for k in CALIBRATION_FEATURES}


class TestPlattCalibrator:
    def test_probability_is_sigmoid_and_monotonic(self):
        cal = PlattCalibrator(a=4.0, b=-2.0)  # crosses 0.5 at top_score=0.5
        assert cal.probability(0.5) == pytest.approx(0.5)
        assert cal.probability(1.0) > cal.probability(0.5) > cal.probability(0.0)
        assert 0.0 <= cal.probability(0.0) <= 1.0

    def test_confidence_uses_top_score(self):
        cal = PlattCalibrator(a=4.0, b=-2.0)
        cands = [_cand("a", 0.9, ["metrics"]), _cand("b", 0.1, ["logs"])]
        assert calibrated_confidence(cal, cands) == pytest.approx(cal.probability(0.9))

    def test_none_for_empty(self):
        assert calibrated_confidence(PlattCalibrator(a=1.0, b=0.0), []) is None

    def test_roundtrip(self):
        cal = PlattCalibrator(a=3.5, b=-1.25)
        restored = calibrator_from_dict(json.loads(json.dumps(cal.to_dict())))
        assert (restored.a, restored.b, restored.feature) == (3.5, -1.25, "top_score")


class TestFromDict:
    def test_rejects_unknown_type_or_version(self):
        with pytest.raises(ValueError):
            calibrator_from_dict({"version": 1, "type": "gbc", "a": 1.0, "b": 0.0})
        with pytest.raises(ValueError):
            calibrator_from_dict({"version": 99, "type": "platt", "a": 1.0, "b": 0.0})

    def test_rejects_unknown_feature(self):
        with pytest.raises(ValueError):
            calibrator_from_dict({"version": 1, "type": "platt", "feature": "bogus", "a": 1.0, "b": 0.0})


class TestLoadCalibrator:
    def test_absent_falls_back_to_none(self, tmp_path):
        assert load_calibrator(None) is None
        assert load_calibrator("") is None
        assert load_calibrator(str(tmp_path / "nope.json")) is None

    def test_unparseable_falls_back(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        assert load_calibrator(str(p)) is None

    def test_loads_valid_artifact(self, tmp_path):
        p = tmp_path / "cal.json"
        p.write_text(json.dumps(PlattCalibrator(a=2.0, b=-0.5).to_dict()))
        cal = load_calibrator(str(p))
        assert isinstance(cal, PlattCalibrator)
        assert cal.a == 2.0
