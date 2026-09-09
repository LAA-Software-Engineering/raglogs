"""Integration test for rare_event trigger detection wired into assemble_evidence
(#82 T2): the linkage gate promotes a same-service rare change to trigger_explains
and demotes an unrelated one to trigger_found only. Needs Postgres (build_service_graph
issues a query); clusters are constructed directly so the test is deterministic."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
WIN_END = T0 + timedelta(seconds=600)
SCOPE = "eval:trigger-rare-event"


@pytest.fixture
def db_session():
    from sqlalchemy import text

    from src.db.models import Base
    from src.db.session import check_connection, get_db, get_engine

    if not check_connection():
        pytest.skip("Cannot connect to database")
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with get_db() as db:
        yield db


def _cluster(fp, msg, services, *, count, baseline_count, change_ratio, first_seen, error_services=None):
    from src.core.clustering.clusterer import ClusterData

    return ClusterData(
        fingerprint=fp, representative_message=msg, count=count, services=services,
        levels={"error": count} if error_services else {"info": count},
        first_seen=first_seen, last_seen=first_seen, baseline_count=baseline_count,
        change_ratio=change_ratio, importance_score=float(count),
        error_service_counts=error_services or {},
    )


def _clusters(trigger_service):
    # primary: an established error cluster on cart (onset at T0+30s)
    primary = _cluster("err", "NullPointer in cart", {"cart": 80}, count=80, baseline_count=3,
                       change_ratio=20.0, first_seen=T0 + timedelta(seconds=30),
                       error_services={"cart": 80})
    # a novel (baseline_count 0) rare change just before onset, on trigger_service
    trigger = _cluster("cfg", "config reloaded", {trigger_service: 1}, count=1, baseline_count=0,
                       change_ratio=1.0, first_seen=T0 + timedelta(seconds=5))
    return [primary, trigger]


def _assemble(db, clusters, monkeypatch):
    from src.config import reload_settings
    from src.core.explain.evidence import assemble_evidence

    monkeypatch.setenv("TRIGGER_MODE", "rare_event")
    reload_settings()
    try:
        return assemble_evidence(db, T0, WIN_END, clusters, scope=SCOPE)
    finally:
        monkeypatch.delenv("TRIGGER_MODE", raising=False)
        reload_settings()


def test_same_service_rare_change_explains(db_session, monkeypatch):
    # no traces -> linkage falls back to same-service overlap; cart change on a
    # cart error -> validated.
    pkt = _assemble(db_session, _clusters("cart"), monkeypatch)
    assert pkt.trigger_found is True
    assert pkt.trigger_explains is True
    assert any(t.service == "cart" for t in pkt.trigger_candidates)


def test_unrelated_service_rare_change_found_not_explained(db_session, monkeypatch):
    # a rare change on an unrelated service with no trace linkage -> found, but
    # not offered as the cause (skip, not fail).
    pkt = _assemble(db_session, _clusters("billing"), monkeypatch)
    assert pkt.trigger_found is True
    assert pkt.trigger_explains is False
