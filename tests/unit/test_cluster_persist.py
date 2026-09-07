"""Unit tests for cluster/member row building (the bulk-insert path, #85)."""

import uuid

from src.core.clustering.clusterer import (
    _MAX_CLUSTER_MEMBERS,
    ClusterData,
    _cluster_and_member_rows,
)


def _cluster(fingerprint: str, n_members: int, message: str = "err") -> ClusterData:
    return ClusterData(
        fingerprint=fingerprint,
        representative_message=message,
        count=n_members,
        services={"api": n_members},
        levels={"error": n_members},
        first_seen=None,
        last_seen=None,
        baseline_count=0,
        change_ratio=1.0,
        importance_score=1.0,
        log_entry_ids=[uuid.uuid4() for _ in range(n_members)],
    )


def test_one_cluster_row_per_cluster():
    run_id = uuid.uuid4()
    clusters = [_cluster("fp1", 3), _cluster("fp2", 5)]
    cluster_rows, _ = _cluster_and_member_rows(run_id, clusters)
    assert len(cluster_rows) == 2
    assert {r["fingerprint"] for r in cluster_rows} == {"fp1", "fp2"}
    assert all(r["cluster_run_id"] == run_id for r in cluster_rows)


def test_members_reference_their_cluster():
    clusters = [_cluster("fp1", 2), _cluster("fp2", 4)]
    cluster_rows, member_rows = _cluster_and_member_rows(uuid.uuid4(), clusters)
    valid_ids = {r["id"] for r in cluster_rows}
    assert len(member_rows) == 6
    assert all(m["cluster_id"] in valid_ids for m in member_rows)
    # member ids are unique
    assert len({m["id"] for m in member_rows}) == 6


def test_members_capped_per_cluster():
    clusters = [_cluster("fp1", _MAX_CLUSTER_MEMBERS + 50)]
    _, member_rows = _cluster_and_member_rows(uuid.uuid4(), clusters)
    assert len(member_rows) == _MAX_CLUSTER_MEMBERS


def test_long_representative_message_truncated():
    clusters = [_cluster("fp1", 1, message="x" * 5000)]
    cluster_rows, _ = _cluster_and_member_rows(uuid.uuid4(), clusters)
    assert len(cluster_rows[0]["representative_message"]) == 2048


def test_empty_clusters_produce_no_rows():
    cluster_rows, member_rows = _cluster_and_member_rows(uuid.uuid4(), [])
    assert cluster_rows == []
    assert member_rows == []
