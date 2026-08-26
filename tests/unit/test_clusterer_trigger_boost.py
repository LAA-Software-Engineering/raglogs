"""The trigger-correlation importance boost must apply in the default
(fingerprint-only, embeddings-disabled) clustering path.

_run_clustering computed importance with is_trigger_correlated=False and a
"refined below after sorting" comment promising a later fix-up that never
existed, so trigger-correlated clusters never got the scoring.py
trigger_weight boost unless they happened to be merged by the (opt-in)
semantic-merge path.
"""

import uuid
from datetime import datetime, timezone

from src.core.clustering.baseline import compute_change_ratio
from src.core.clustering.clusterer import _build_cluster_data
from src.core.clustering.scoring import compute_importance_score


def _group(message: str) -> dict:
    return {
        "messages": [message] * 5,
        "services": {"api": 5},
        "levels": {"error": 5},
        "timestamps": [datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)],
        "ids": [uuid.uuid4() for _ in range(5)],
    }


def test_trigger_message_sets_is_trigger_and_boosts_importance():
    cluster = _build_cluster_data("fp-trigger", _group("deployment started"), {})

    assert cluster.is_trigger is True

    expected = compute_importance_score(
        count=5,
        levels_distribution={"error": 5},
        change_ratio=compute_change_ratio(5, 0),
        services_count=1,
        is_trigger_correlated=True,
    )
    assert cluster.importance_score == expected


def test_non_trigger_message_gets_no_boost():
    cluster = _build_cluster_data("fp-plain", _group("request handled"), {})

    assert cluster.is_trigger is False

    expected = compute_importance_score(
        count=5,
        levels_distribution={"error": 5},
        change_ratio=compute_change_ratio(5, 0),
        services_count=1,
        is_trigger_correlated=False,
    )
    assert cluster.importance_score == expected


def test_trigger_cluster_outranks_otherwise_identical_non_trigger_cluster():
    trigger_cluster = _build_cluster_data("fp-a", _group("deployment started"), {})
    plain_cluster = _build_cluster_data("fp-b", _group("request handled"), {})

    assert trigger_cluster.importance_score == plain_cluster.importance_score + 2.0
