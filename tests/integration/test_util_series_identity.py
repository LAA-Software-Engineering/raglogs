"""Integration: #209 M2b series identity on the PRODUCT path (the metric_samples rows
build_structural_view reads). Two series of one metric at the same timestamp must both persist, and a
per-mode gauge must come out UNKNOWN — never a measured ABSENT from whichever row landed first.
Skipped without Postgres."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)

W = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
BASE = W - timedelta(seconds=300)
END = W + timedelta(seconds=300)
SCOPE = "eval:util-series"


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


def _series(service, metric, base, inc, attributes, metric_type="gauge"):
    from src.core.ingestion.telemetry import ParsedMetricSample
    out = []
    for i in range(1, 6):
        out.append(ParsedMetricSample(service=service, metric=metric, value=base, ts=W - timedelta(seconds=30 * i),
                                      metric_type=metric_type, attributes=attributes))
        out.append(ParsedMetricSample(service=service, metric=metric, value=inc, ts=W + timedelta(seconds=30 * i),
                                      metric_type=metric_type, attributes=attributes))
    return out


def _rows(db):
    from sqlalchemy import select

    from src.db.models import MetricSample
    return db.execute(select(MetricSample).where(MetricSample.scope == SCOPE)).scalars().all()


def test_per_mode_gauge_persists_both_series_and_is_unknown(db_session):
    from src.core.ingestion.telemetry import persist_metric_samples
    from src.core.rca.structural_model import structural_signals

    # saturated CPU split by cpu.mode: idle 0.9 -> 0.1, user 0.1 -> 0.9 (both means stay 0.5)
    inst = {"service.instance.id": "pod-1"}
    persist_metric_samples(db_session, _series("host", "system.cpu.utilization", 0.9, 0.1, {"cpu.mode": "idle", **inst})
                           + _series("host", "system.cpu.utilization", 0.1, 0.9, {"cpu.mode": "user", **inst}),
                           scope=SCOPE)
    db_session.flush()
    rows = _rows(db_session)
    assert len(rows) == 20  # both series survive at every shared timestamp (none dropped on conflict)

    util = structural_signals([], rows, W).util_signals
    assert util[("host", "cpu")].sig_state is None  # UNKNOWN, not an averaged "measured normal"


def test_single_verified_series_through_the_db_is_measured(db_session):
    from src.core.ingestion.telemetry import persist_metric_samples
    from src.core.rca.structural import build_structural_view

    persist_metric_samples(db_session, _series("ad", "jvm.cpu.recent_utilization", 0.01, 0.9,
                                               {"service.instance.id": "ad-1"}), scope=SCOPE)
    db_session.flush()
    built = build_structural_view(db_session, SCOPE, W, END, BASE)
    assert built is not None
    result, _ranking = built
    assert "ad" in result.localization


def test_identity_less_rows_are_never_measured(db_session):
    from src.core.ingestion.telemetry import persist_metric_samples
    from src.core.rca.structural import build_structural_view

    persist_metric_samples(db_session, _series("ad", "jvm.cpu.recent_utilization", 0.01, 0.9, None), scope=SCOPE)
    db_session.flush()
    assert build_structural_view(db_session, SCOPE, W, END, BASE) is None  # no evidence claimed


def _replicas_without_instance(offset_s=5):
    """Two replicas whose resource names no instance (identity {}), offset timestamps: A calm, B pegged."""
    from src.core.ingestion.telemetry import ParsedMetricSample
    out = []
    for value_base, value_inc, off in ((0.40, 0.45, 0), (0.40, 0.90, offset_s)):
        for i in range(1, 6):
            out.append(ParsedMetricSample(service="ad", metric="jvm.cpu.recent_utilization", value=value_base,
                                          ts=W - timedelta(seconds=30 * i - off), metric_type="gauge",
                                          attributes={}))
            out.append(ParsedMetricSample(service="ad", metric="jvm.cpu.recent_utilization", value=value_inc,
                                          ts=W + timedelta(seconds=30 * i + off), metric_type="gauge",
                                          attributes={}))
    return out


def test_replicas_without_a_named_instance_are_unknown_from_the_db(db_session):
    from src.core.ingestion.telemetry import persist_metric_samples
    from src.core.rca.structural_model import structural_signals

    persist_metric_samples(db_session, _replicas_without_instance(), scope=SCOPE)
    db_session.flush()
    rows = _rows(db_session)
    assert len(rows) == 20  # both replicas stored (distinct timestamps)
    assert structural_signals([], rows, W).util_signals[("ad", "cpu")].sig_state is None


def test_default_path_reducers_see_every_series_from_the_db(db_session):
    """The trigger onsets and ranker features (default explain path) read the rows metric_pk now keeps
    per series; they must reduce per series, not average cpu.mode idle/user into a flat 0.5."""
    from src.core.explain.evidence import _metric_onsets
    from src.core.ingestion.telemetry import persist_metric_samples
    from src.core.rca.features import compute_features

    inst = {"service.instance.id": "pod-1"}
    persist_metric_samples(db_session, _series("host", "system.cpu.utilization", 0.9, 0.1, {"cpu.mode": "idle", **inst})
                           + _series("host", "system.cpu.utilization", 0.1, 0.9, {"cpu.mode": "user", **inst}),
                           scope=SCOPE)
    db_session.flush()
    onsets = _metric_onsets(db_session, SCOPE, W, END)
    assert any(o.service == "host" and o.metric == "system.cpu.utilization" for o in onsets)
    table = compute_features(db_session, SCOPE, incident_start=W, incident_end=END, baseline_start=BASE)
    assert {f.service: f.met_anom for f in table.services}["host"] > 1.0
