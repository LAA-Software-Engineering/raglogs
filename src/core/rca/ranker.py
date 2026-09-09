"""Learned multi-modal RCA ranker — inference side (#118 C2).

Scores a :class:`~src.core.rca.features.ServiceFeatures` row so the top-scored
service is the predicted root cause. The model is a gradient-boosted tree
ensemble trained offline (``scripts/train_rca_ranker.py``) and serialised as a
**non-pickle JSON artifact** — plain arrays for each regression tree plus the
ensemble's init score and learning rate. Inference is a pure-Python tree walk
with **no sklearn / numpy dependency at runtime**, so:

- loading a model never executes arbitrary code (unlike pickle), and
- raglogs still runs with only its base deps — sklearn is a *training*-time need.

Graceful fallback is by design: :func:`load_ranker` returns ``None`` when no
artifact is configured or the file is missing/unreadable, and the caller then
uses the existing volume-based selector. The fallback keys on **model-artifact
absence**, not on sklearn being importable (a deployment can ship the JSON
without sklearn and still rank).

Artifact schema (``version`` 1)::

    {
      "version": 1,
      "feature_names": ["log_err", ...],   # input order
      "init": <float>,                      # ensemble initial raw score F0
      "learning_rate": <float>,
      "trees": [                            # each a binary regression tree
        {"children_left": [...], "children_right": [...],
         "feature": [...], "threshold": [...], "value": [...]}
      ]
    }

``decision_function(x) = init + learning_rate * sum(tree(x))`` and
``P(root cause) = sigmoid(decision_function(x))`` — the binary GBM contract.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.core.rca.features import ServiceFeatures

ARTIFACT_VERSION = 1

# Sentinel in ``children_left`` marking a leaf (sklearn uses -1).
_LEAF = -1


@dataclass
class _Tree:
    children_left: list[int]
    children_right: list[int]
    feature: list[int]
    threshold: list[float]
    value: list[float]

    def predict(self, x: list[float]) -> float:
        node = 0
        while self.children_left[node] != _LEAF:
            if x[self.feature[node]] <= self.threshold[node]:
                node = self.children_left[node]
            else:
                node = self.children_right[node]
        return self.value[node]


@dataclass
class RcaRanker:
    feature_names: list[str]
    init: float
    learning_rate: float
    trees: list[_Tree]

    def decision_function(self, x: list[float]) -> float:
        return self.init + self.learning_rate * sum(t.predict(x) for t in self.trees)

    def score_vector(self, x: list[float]) -> float:
        """P(root cause) for a raw feature vector already in ``feature_names`` order."""
        return 1.0 / (1.0 + math.exp(-self.decision_function(x)))

    def score(self, sf: ServiceFeatures) -> float:
        """P(``sf`` is the root cause) in [0, 1]."""
        return self.score_vector(sf.vector(self.feature_names))


def from_dict(data: dict) -> RcaRanker:
    """Build a ranker from a parsed artifact dict (raises on a bad schema)."""
    version = data.get("version")
    if version != ARTIFACT_VERSION:
        raise ValueError(f"unsupported RCA ranker artifact version: {version!r}")
    trees = [
        _Tree(
            children_left=list(t["children_left"]),
            children_right=list(t["children_right"]),
            feature=list(t["feature"]),
            threshold=[float(v) for v in t["threshold"]],
            value=[float(v) for v in t["value"]],
        )
        for t in data["trees"]
    ]
    return RcaRanker(
        feature_names=list(data["feature_names"]),
        init=float(data["init"]),
        learning_rate=float(data["learning_rate"]),
        trees=trees,
    )


def serialize_gbc(clf, feature_names: list[str]) -> dict:
    """Serialise a fitted binary ``GradientBoostingClassifier`` into a v1 artifact
    dict (training-time helper). Duck-typed on the fitted estimator so this
    module needs no sklearn import; numpy is imported lazily only here.

    The ensemble's initial raw score ``F0`` is recovered exactly from the identity
    ``decision_function(x) = F0 + lr * sum(tree(x))`` evaluated at a reference row
    (``F0`` is constant), so the pure-Python evaluator reproduces
    ``clf.decision_function`` / ``predict_proba`` to numerical precision.
    """
    import numpy as np

    lr = float(clf.learning_rate)
    trees = []
    for stage in clf.estimators_:
        t = stage[0].tree_
        trees.append(
            {
                "children_left": [int(v) for v in t.children_left],
                "children_right": [int(v) for v in t.children_right],
                "feature": [int(v) for v in t.feature],
                "threshold": [float(v) for v in t.threshold],
                "value": [float(v) for v in t.value.reshape(-1)],
            }
        )
    # Recover F0 from a reference row: F0 = decision_function(x0) - lr*sum(tree(x0)).
    x0 = [0.0] * len(feature_names)
    ranker_trees = [
        _Tree(tr["children_left"], tr["children_right"], tr["feature"], tr["threshold"], tr["value"])
        for tr in trees
    ]
    trees_sum = sum(t.predict(x0) for t in ranker_trees)
    df0 = float(clf.decision_function(np.array([x0], dtype=float))[0])
    init = df0 - lr * trees_sum
    return {
        "version": ARTIFACT_VERSION,
        "feature_names": list(feature_names),
        "init": init,
        "learning_rate": lr,
        "trees": trees,
    }


def load_ranker(path: Optional[str]) -> Optional[RcaRanker]:
    """Load the ranker from ``path``, or return ``None`` for graceful fallback.

    ``None`` is returned when ``path`` is empty/None, the file does not exist, or
    it cannot be parsed into a valid artifact — in every case the caller falls
    back to the volume-based selector. Absence of a model is normal, not an error.
    """
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return from_dict(json.loads(p.read_text()))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
