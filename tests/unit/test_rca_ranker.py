"""Unit tests for the learned RCA ranker (#118 C2): pure-Python evaluator parity
with sklearn, artifact round-trip, and graceful fallback. No database."""
import json

import pytest

from src.core.rca.features import FEATURE_NAMES, ServiceFeatures
from src.core.rca.ranker import (
    ARTIFACT_VERSION,
    RcaRanker,
    from_dict,
    load_ranker,
    serialize_gbc,
)


def _fit_toy_gbc(seed=0, n=200):
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier

    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, len(FEATURE_NAMES)))
    # A learnable signal: label depends on met_anom + log_grp (top spike features).
    idx_anom = FEATURE_NAMES.index("met_anom")
    idx_grp = FEATURE_NAMES.index("log_grp")
    y = ((X[:, idx_anom] + X[:, idx_grp]) > 0).astype(int)
    clf = GradientBoostingClassifier(random_state=seed).fit(X, y)
    return clf, X


class TestSklearnParity:
    def test_pure_python_reproduces_predict_proba(self):
        clf, X = _fit_toy_gbc()
        ranker = from_dict(serialize_gbc(clf, FEATURE_NAMES))
        proba = clf.predict_proba(X)[:, 1]
        max_err = max(abs(ranker.score_vector(X[i].tolist()) - proba[i]) for i in range(len(X)))
        assert max_err < 1e-6

    def test_artifact_is_plain_json_serializable(self):
        clf, _ = _fit_toy_gbc()
        artifact = serialize_gbc(clf, FEATURE_NAMES)
        # round-trips through json without custom encoders (no pickle, no numpy types)
        restored = json.loads(json.dumps(artifact))
        assert restored["version"] == ARTIFACT_VERSION
        assert restored["feature_names"] == FEATURE_NAMES
        assert isinstance(restored["init"], float)
        assert len(restored["trees"]) == len(clf.estimators_)


class TestScore:
    def test_score_uses_feature_name_order(self):
        clf, _ = _fit_toy_gbc()
        ranker = from_dict(serialize_gbc(clf, FEATURE_NAMES))
        low = ServiceFeatures(service="a", met_anom=-3.0, log_grp=0)
        high = ServiceFeatures(service="b", met_anom=5.0, log_grp=5)
        assert 0.0 <= ranker.score(low) <= 1.0
        assert ranker.score(high) > ranker.score(low)  # stronger signal ranks higher


class TestFromDict:
    def test_rejects_unknown_version(self):
        with pytest.raises(ValueError):
            from_dict({"version": 999, "feature_names": [], "init": 0.0,
                       "learning_rate": 0.1, "trees": []})

    def test_single_leaf_tree_returns_init(self):
        # A degenerate tree (root is a leaf) contributes its leaf value.
        ranker = from_dict({
            "version": 1, "feature_names": FEATURE_NAMES, "init": 0.0, "learning_rate": 1.0,
            "trees": [{"children_left": [-1], "children_right": [-1], "feature": [-2],
                       "threshold": [-2.0], "value": [0.0]}],
        })
        assert ranker.score_vector([0.0] * len(FEATURE_NAMES)) == pytest.approx(0.5)


class TestLoadRanker:
    def test_none_and_missing_path_fall_back(self, tmp_path):
        assert load_ranker(None) is None
        assert load_ranker("") is None
        assert load_ranker(str(tmp_path / "nope.json")) is None

    def test_unparseable_artifact_falls_back(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        assert load_ranker(str(p)) is None
        p.write_text(json.dumps({"version": 1}))  # missing required keys
        assert load_ranker(str(p)) is None

    def test_loads_a_valid_artifact(self, tmp_path):
        clf, _ = _fit_toy_gbc()
        p = tmp_path / "model.json"
        p.write_text(json.dumps(serialize_gbc(clf, FEATURE_NAMES)))
        ranker = load_ranker(str(p))
        assert isinstance(ranker, RcaRanker)
        assert ranker.feature_names == FEATURE_NAMES
