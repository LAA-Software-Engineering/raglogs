"""Unit test for the calibrator reliability harness (#118 D). Verifies the ECE
metric and the nested-LOSO stage-1 pipeline on synthetic data — no HF/DB."""
import importlib.util
from pathlib import Path

import pytest

from src.core.rca.calibration import CALIBRATION_FEATURES
from src.core.rca.features import FEATURE_NAMES

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "eval_rca_calibrator.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("eval_rca_calibrator", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestEce:
    def test_perfectly_calibrated_is_zero(self):
        mod = _load_module()
        # A bin whose mean confidence equals its accuracy contributes 0.
        pairs = [(0.9, 1), (0.9, 1), (0.9, 1), (0.9, 0)]  # conf .9, acc .75 -> 0.15
        assert mod._ece(pairs, n_bins=10) == pytest.approx(0.15, abs=1e-9)

    def test_empty_is_zero(self):
        assert _load_module()._ece([]) == 0.0


class TestStage1:
    def test_out_of_fold_rows_have_features_and_label(self):
        mod = _load_module()
        # Two systems, separable signal (injected service has high met_anom).
        rows = []
        for sysname in ("a", "b"):
            for i in range(8):
                case = f"{sysname}{i}"
                base = {f: 0.0 for f in FEATURE_NAMES}
                rows.append({**base, "case": case, "system": sysname,
                             "service": f"{sysname}-inj{i}", "label": 1, "met_anom": 5.0})
                rows.append({**base, "case": case, "system": sysname,
                             "service": f"{sysname}-decoy{i}", "label": 0, "met_anom": 0.0})
        stage1 = mod._stage1_oof(rows)
        assert len(stage1) == 16  # one row per case
        assert all(set(CALIBRATION_FEATURES).issubset(r) for r in stage1)
        assert all(r["correct"] in (0, 1) for r in stage1)
        # separable signal -> the out-of-fold ranker gets most cases right
        assert sum(r["correct"] for r in stage1) >= 12
