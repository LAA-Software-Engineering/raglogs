"""Equivalence gate for server-side clustering aggregation (#85).

``_run_clustering`` groups a window's rows per fingerprint in Postgres
(``_aggregate_groups``) instead of fetching every row and grouping in Python
(``_cluster_select`` + ``_group_rows``, kept as the reference oracle). This test
proves the two produce **identical** ``ClusterData`` — count, services, levels,
error-service counts, first/last seen, representative message, importance,
change_ratio, is_trigger — across adversarial row shapes, and that the member
sample is a valid capped subset. If these ever diverge, the optimization is wrong.

Skipped without a live Postgres (CI runs integration with a DB).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)

WINDOW_START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
WINDOW_END = WINDOW_START + timedelta(hours=1)


@pytest.fixture
def db_session():
    from src.db.models import Base
    from src.db.session import check_connection, get_db, get_engine

    if not check_connection():
        pytest.skip("Cannot connect to database")

    engine = get_engine()
    from sqlalchemy import text

    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with get_db() as db:
        yield db


def _row(scope, fp, i, *, service, level, message, ts=None):
    from src.db.models import LogEntry

    return LogEntry(
        id=uuid.uuid4(),
        timestamp=ts or (WINDOW_START + timedelta(seconds=i)),
        service=service,
        level=level,
        normalized_message=message,
        raw_message=None,
        fingerprint=fp,
        source_adapter="file",
        scope=scope,
    )


def _insert(db, rows):
    for r in rows:
        db.add(r)
    db.flush()


def _oracle_groups(db, scope):
    """The reference: fetch every in-window row and group in Python."""
    from src.core.clustering.clusterer import _cluster_select, _group_rows
    from src.db.models import LogEntry
    from src.db.scope_filter import filter_log_entries_by_scope

    q = _cluster_select().where(
        LogEntry.timestamp >= WINDOW_START,
        LogEntry.timestamp <= WINDOW_END,
        LogEntry.fingerprint.isnot(None),
    )
    q = filter_log_entries_by_scope(q, scope)
    return _group_rows(db.execute(q).all())


def _assert_equivalent(db, scope, *, cap):
    """Build ClusterData both ways (same baseline) and assert full equivalence."""
    from src.core.clustering.clusterer import _aggregate_groups, _build_cluster_data

    old_groups = _oracle_groups(db, scope)
    new_groups = _aggregate_groups(
        db, WINDOW_START, WINDOW_END,
        scope=scope, service=None, environment=None, ingestion_job_id=None,
    )
    # Same baseline for both, so only the grouping is under test.
    baseline: dict[str, int] = {}
    old = {c.fingerprint: c for c in (_build_cluster_data(fp, g, baseline) for fp, g in old_groups.items())}
    new = {c.fingerprint: c for c in (_build_cluster_data(fp, g, baseline) for fp, g in new_groups.items())}

    assert set(old) == set(new), f"fingerprint sets differ: {set(old) ^ set(new)}"
    full_ids = {c.fingerprint: set(c.log_entry_ids) for c in old.values()}
    for fp in old:
        o, n = old[fp], new[fp]
        assert n.count == o.count, f"{fp}: count {n.count} != {o.count}"
        assert n.services == o.services, f"{fp}: services {n.services} != {o.services}"
        assert n.levels == o.levels, f"{fp}: levels {n.levels} != {o.levels}"
        assert n.error_service_counts == o.error_service_counts, f"{fp}: error_service_counts"
        assert n.first_seen == o.first_seen, f"{fp}: first_seen"
        assert n.last_seen == o.last_seen, f"{fp}: last_seen"
        assert n.representative_message == o.representative_message, f"{fp}: rep_message"
        assert n.is_trigger == o.is_trigger, f"{fp}: is_trigger"
        assert n.change_ratio == pytest.approx(o.change_ratio), f"{fp}: change_ratio"
        assert n.importance_score == pytest.approx(o.importance_score), f"{fp}: importance"
        # Member sample: a distinct, capped subset of the true membership.
        assert len(n.log_entry_ids) == len(set(n.log_entry_ids)), f"{fp}: dup member ids"
        assert set(n.log_entry_ids).issubset(full_ids[fp]), f"{fp}: members not a subset"
        assert len(n.log_entry_ids) == min(n.count, cap), f"{fp}: member sample size"


def test_mixed_services_levels_and_errors(db_session):
    from src.core.clustering.clusterer import _MAX_CLUSTER_MEMBERS

    scope = "agg:mixed"
    rows = []
    # fp1: two services, error + info; one service is error-heavy
    for i in range(5):
        rows.append(_row(scope, "fp1", i, service="api", level="error", message="db timeout"))
    for i in range(5, 8):
        rows.append(_row(scope, "fp1", i, service="api", level="info", message="db timeout"))
    for i in range(8, 10):
        rows.append(_row(scope, "fp1", i, service="web", level="warn", message="db timeout"))
    # fp2: single service, FATAL/Critical mixed case (case-insensitive error match)
    for i in range(3):
        rows.append(_row(scope, "fp2", 100 + i, service="worker", level="FATAL", message="oom killed"))
    rows.append(_row(scope, "fp2", 200, service="worker", level="Critical", message="oom killed"))
    _insert(db_session, rows)

    _assert_equivalent(db_session, scope, cap=_MAX_CLUSTER_MEMBERS)


def test_null_service_null_level_and_empty_message(db_session):
    from src.core.clustering.clusterer import _MAX_CLUSTER_MEMBERS

    scope = "agg:nulls"
    rows = [
        _row(scope, "fpn", 0, service=None, level="info", message="msg a"),
        _row(scope, "fpn", 1, service=None, level=None, message="msg a"),
        _row(scope, "fpn", 2, service="api", level="info", message="msg a"),
        _row(scope, "fpn", 3, service="api", level="info", message=""),      # empty msg
        _row(scope, "fpn", 4, service="api", level="info", message=None),    # null msg
        # a fingerprint whose rows ALL have empty/null messages -> rep = ""
        _row(scope, "fpe", 5, service="api", level="info", message=""),
        _row(scope, "fpe", 6, service="api", level=None, message=None),
    ]
    _insert(db_session, rows)
    _assert_equivalent(db_session, scope, cap=_MAX_CLUSTER_MEMBERS)


def test_empty_string_service_and_level(db_session):
    """Empty-string service/level (schema-permitted, though the parser normalizes it
    away before persist) must be dropped identically by both paths — _group_rows uses
    truthiness (`if service:`), so queries B/C exclude "" too (#192 review)."""
    from src.core.clustering.clusterer import _MAX_CLUSTER_MEMBERS

    scope = "agg:emptystr"
    rows = [
        _row(scope, "fpx", 0, service="", level="error", message="m"),      # "" service
        _row(scope, "fpx", 1, service="api", level="", message="m"),        # "" level
        _row(scope, "fpx", 2, service="api", level="error", message="m"),
        _row(scope, "fpx", 3, service="", level="", message="m"),           # both ""
    ]
    _insert(db_session, rows)
    _assert_equivalent(db_session, scope, cap=_MAX_CLUSTER_MEMBERS)


def test_timestamp_ties_and_single_row(db_session):
    from src.core.clustering.clusterer import _MAX_CLUSTER_MEMBERS

    scope = "agg:ties"
    tie = WINDOW_START + timedelta(minutes=5)
    rows = [
        _row(scope, "fptie", 0, service="api", level="error", message="boom", ts=tie),
        _row(scope, "fptie", 1, service="api", level="error", message="boom", ts=tie),
        _row(scope, "fptie", 2, service="api", level="error", message="boom", ts=tie),
        _row(scope, "fpsolo", 3, service="db", level="warn", message="slow query"),  # single row
    ]
    _insert(db_session, rows)
    _assert_equivalent(db_session, scope, cap=_MAX_CLUSTER_MEMBERS)


def test_member_sample_cap_exceeded(db_session):
    from src.core.clustering.clusterer import _MAX_CLUSTER_MEMBERS

    scope = "agg:cap"
    n = _MAX_CLUSTER_MEMBERS * 2 + 37  # comfortably over the cap
    rows = [_row(scope, "big", i, service="api", level="info", message="noisy line") for i in range(n)]
    _insert(db_session, rows)
    _assert_equivalent(db_session, scope, cap=_MAX_CLUSTER_MEMBERS)


def test_representative_message_frequency_tie_is_a_mode(db_session):
    """On an exact frequency tie the representative may differ from the row-scan
    path's arbitrary pick — but must still be one of the most-common messages."""
    from src.core.clustering.clusterer import _aggregate_groups

    scope = "agg:msgtie"
    rows = [
        _row(scope, "fptie", 0, service="api", level="info", message="alpha"),
        _row(scope, "fptie", 1, service="api", level="info", message="alpha"),
        _row(scope, "fptie", 2, service="api", level="info", message="beta"),
        _row(scope, "fptie", 3, service="api", level="info", message="beta"),
    ]
    _insert(db_session, rows)
    groups = _aggregate_groups(
        db_session, WINDOW_START, WINDOW_END,
        scope=scope, service=None, environment=None, ingestion_job_id=None,
    )
    rep = groups["fptie"]["messages"][0]
    assert rep in {"alpha", "beta"}  # both occur twice; either is a valid mode


def test_run_clustering_end_to_end_matches_oracle_ordering(db_session):
    """Full _run_clustering (now aggregation-backed) yields the same clusters,
    ordering, and primary selection as building from the oracle groups."""
    from src.core.clustering.clusterer import (
        _build_cluster_data,
        _run_clustering,
        rank_and_merge_clusters,
    )
    from src.core.explain.evidence import select_primary_cluster

    scope = "agg:e2e"
    rows = []
    for i in range(12):
        rows.append(_row(scope, "cause", i, service="payments", level="error", message="upstream 500"))
    for i in range(40):
        rows.append(_row(scope, "cascade", 100 + i, service="checkout", level="error", message="dependency failed"))
    for i in range(200):
        rows.append(_row(scope, "noise", 500 + i, service="web", level="info", message="ok"))
    _insert(db_session, rows)

    _, clusters = _run_clustering(
        db_session, WINDOW_START, WINDOW_END, save_to_db=False, scope=scope,
    )
    # Oracle: build from row-scan groups, rank the same way.
    old_groups = _oracle_groups(db_session, scope)
    oracle = [_build_cluster_data(fp, g, {}) for fp, g in old_groups.items()]
    oracle_ranked, _ = rank_and_merge_clusters(oracle, max_clusters=50)

    assert [c.fingerprint for c in clusters] == [c.fingerprint for c in oracle_ranked]
    got_primary = select_primary_cluster(clusters)
    want_primary = select_primary_cluster(oracle_ranked)
    assert (got_primary.fingerprint if got_primary else None) == (
        want_primary.fingerprint if want_primary else None
    )
