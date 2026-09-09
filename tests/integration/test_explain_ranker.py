"""Integration test: the learned RCA ranker wired into explain_window (#118 C2b).

With a model artifact configured, explain surfaces the ranker's top service (even
one with no distinctive log cluster); with no model it is skipped and the
log-cluster path is unchanged. Skipped without Postgres."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)

INJECT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
WINDOW_END = INJECT + timedelta(seconds=600)
BASELINE_START = INJECT - timedelta(seconds=300)
SCOPE = "eval:explain-ranker-test"


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


def _seed(db):
    """A noisy 'frontend' log cluster (what the log-only path would pick) plus a
    strong metric anomaly on 'paymentservice' (the injected root cause)."""
    from src.core.ingestion.telemetry import ParsedMetricSample, persist_metric_samples
    from src.db.models import LogEntry

    for i in range(20):
        db.add(LogEntry(
            timestamp=INJECT + timedelta(seconds=i), service="frontend", level="error",
            raw_message="500 from upstream", normalized_message="500 from upstream",
            fingerprint="fe", scope=SCOPE,
        ))
    persist_metric_samples(db, [
        ParsedMetricSample(service="paymentservice", metric="cpu", value=1.0, ts=BASELINE_START + timedelta(seconds=5)),
        ParsedMetricSample(service="paymentservice", metric="cpu", value=50.0, ts=INJECT + timedelta(seconds=5)),
        ParsedMetricSample(service="frontend", metric="cpu", value=1.0, ts=BASELINE_START + timedelta(seconds=5)),
        ParsedMetricSample(service="frontend", metric="cpu", value=1.1, ts=INJECT + timedelta(seconds=5)),
    ], scope=SCOPE)
    db.flush()


def _train_model(tmp_path):
    """A ranker that keys on met_anom, serialised to a JSON artifact path."""
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier

    from src.core.rca.features import FEATURE_NAMES
    from src.core.rca.ranker import serialize_gbc

    rng = np.random.default_rng(0)
    X = rng.normal(size=(200, len(FEATURE_NAMES)))
    idx = FEATURE_NAMES.index("met_anom")
    y = (X[:, idx] > 0.5).astype(int)
    clf = GradientBoostingClassifier(random_state=0).fit(X, y)
    p = tmp_path / "model.json"
    p.write_text(json.dumps(serialize_gbc(clf, FEATURE_NAMES)))
    return str(p)


def _write_calibrator(tmp_path):
    import json

    from src.core.rca.calibration import PlattCalibrator

    p = tmp_path / "cal.json"
    p.write_text(json.dumps(PlattCalibrator(a=4.0, b=-2.0).to_dict()))
    return str(p)


def test_ranker_localises_metric_only_root_cause(db_session, tmp_path, monkeypatch):
    from src.config import reload_settings
    from src.core.explain.summarizer import explain_window

    _seed(db_session)
    monkeypatch.setenv("RCA_RANKER_MODEL_PATH", _train_model(tmp_path))
    monkeypatch.setenv("RCA_CALIBRATOR_MODEL_PATH", _write_calibrator(tmp_path))
    reload_settings()
    try:
        result = explain_window(
            db=db_session, window_start=INJECT, window_end=WINDOW_END,
            no_llm=True, baseline_window_str="300s", scope=SCOPE,
        )
        assert result.predicted_root_cause == "paymentservice"  # metric signal, not the noisy log service
        assert result.root_cause_candidates  # candidates exposed
        # a calibrator is configured -> calibrated P(top-1 correct) in [0, 1]
        assert result.predicted_root_cause_confidence is not None
        assert 0.0 <= result.predicted_root_cause_confidence <= 1.0
    finally:
        monkeypatch.delenv("RCA_RANKER_MODEL_PATH", raising=False)
        monkeypatch.delenv("RCA_CALIBRATOR_MODEL_PATH", raising=False)
        reload_settings()


def test_no_model_leaves_log_path_unchanged(db_session):
    from src.config import reload_settings
    from src.core.explain.summarizer import explain_window

    _seed(db_session)
    reload_settings()  # RCA_RANKER_MODEL_PATH unset -> default ""
    result = explain_window(
        db=db_session, window_start=INJECT, window_end=WINDOW_END,
        no_llm=True, baseline_window_str="300s", scope=SCOPE,
    )
    assert result.predicted_root_cause is None
    assert result.root_cause_candidates == []
    assert result.predicted_root_cause_confidence is None
    # the log-cluster path still selects the noisy frontend cluster
    assert result.primary_cluster is not None
    assert "frontend" in result.primary_cluster["services"]
