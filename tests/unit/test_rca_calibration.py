"""Unit tests for RCA confidence calibration (#118 D / #83). No DB."""
import pytest

from src.core.rca.calibration import (
    CALIBRATION_FEATURES,
    calibrated_confidence,
    calibration_features,
    load_calibrator,
)
from src.core.rca.candidates import RootCauseCandidate
from src.core.rca.features import ServiceFeatures


def _cand(service, score, modalities):
    sf = ServiceFeatures(service=service)
    from src.core.rca.candidates import ModalityEvidence

    ev = [ModalityEvidence(modality=m, detail=m) for m in modalities]
    return RootCauseCandidate(service=service, score=score, features=sf, evidence=ev)


class TestCalibrationFeatures:
    def test_margin_and_counts(self):
        cands = [
            _cand("a", 0.8, ["logs", "metrics"]),
            _cand("b", 0.5, ["traces"]),
            _cand("c", 0.1, ["logs"]),
        ]
        f = calibration_features(cands)
        assert f["top_score"] == pytest.approx(0.8)
        assert f["margin"] == pytest.approx(0.3)  # 0.8 - 0.5
        assert f["n_candidates"] == 3.0
        assert f["n_modalities"] == 2.0  # top has logs+metrics
        # present modalities across all = {logs, metrics, traces} = 3; top has 2
        assert f["cross_modal_agreement"] == pytest.approx(2 / 3)

    def test_single_candidate_zero_margin(self):
        f = calibration_features([_cand("a", 0.9, ["metrics"])])
        assert f["margin"] == 0.0
        assert f["cross_modal_agreement"] == pytest.approx(1.0)  # top has the only modality present

    def test_empty_is_all_zero(self):
        f = calibration_features([])
        assert f == {k: 0.0 for k in CALIBRATION_FEATURES}


def _toy_calibrator():
    """A calibrator keyed on margin: wide margin -> confident."""
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier

    from src.core.rca.ranker import from_dict, serialize_gbc

    rng = np.random.default_rng(0)
    X = rng.random((200, len(CALIBRATION_FEATURES)))
    idx = CALIBRATION_FEATURES.index("margin")
    y = (X[:, idx] > 0.5).astype(int)
    clf = GradientBoostingClassifier(random_state=0).fit(X, y)
    return from_dict(serialize_gbc(clf, CALIBRATION_FEATURES))


class TestCalibratedConfidence:
    def test_confidence_in_unit_interval_and_monotonic_in_margin(self):
        cal = _toy_calibrator()
        wide = [_cand("a", 0.95, ["logs", "metrics"]), _cand("b", 0.05, ["traces"])]
        narrow = [_cand("a", 0.55, ["logs"]), _cand("b", 0.5, ["traces"])]
        c_wide = calibrated_confidence(cal, wide)
        c_narrow = calibrated_confidence(cal, narrow)
        assert 0.0 <= c_narrow <= 1.0 and 0.0 <= c_wide <= 1.0
        assert c_wide > c_narrow  # a wider margin reads as more confident

    def test_none_for_empty(self):
        assert calibrated_confidence(_toy_calibrator(), []) is None

    def test_uses_calibrator_feature_order(self):
        # A calibrator declaring a subset/reordered feature_names still scores.
        cal = _toy_calibrator()
        cal.feature_names = list(reversed(CALIBRATION_FEATURES))
        assert calibrated_confidence(cal, [_cand("a", 0.9, ["metrics"])]) is not None


class TestLoadCalibrator:
    def test_absent_falls_back_to_none(self, tmp_path):
        assert load_calibrator(None) is None
        assert load_calibrator(str(tmp_path / "nope.json")) is None
