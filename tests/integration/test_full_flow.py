"""
Integration tests that run against a real PostgreSQL database.
Requires RAGLOGS_DB_URL to point to a test database.
"""
import os
import pytest
from datetime import datetime, timezone
from pathlib import Path

# Skip all integration tests if no DB is configured
pytestmark = pytest.mark.skipif(
    not os.getenv("RAGLOGS_DB_URL") and not os.getenv("RAGLOGS_INTEGRATION_TESTS"),
    reason="Integration tests require RAGLOGS_DB_URL environment variable",
)


@pytest.fixture(scope="module")
def db_session():
    from src.db.session import get_db, check_connection
    from src.db.models import Base
    from src.db.session import get_engine

    if not check_connection():
        pytest.skip("Cannot connect to database")

    # Create tables
    engine = get_engine()
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.create_all(engine)

    with get_db() as db:
        yield db


@pytest.fixture
def sample_logs_path():
    return Path(__file__).parent.parent.parent / "sample_data" / "sample_incident"


def test_ingest_sample_incident(db_session, sample_logs_path):
    from src.core.ingestion.service import ingest_files

    paths = [str(sample_logs_path)]
    job, stats = ingest_files(
        db=db_session,
        paths=paths,
        recursive=False,
        fmt="json",
    )

    assert job.status == "completed"
    assert stats.parsed_count > 0
    assert stats.lines_read > 0
    assert "billing-worker" in stats.services_detected or "api" in stats.services_detected


def test_cluster_generation(db_session):
    from datetime import timedelta
    from src.core.clustering.clusterer import run_clustering

    window_end = datetime.now(tz=timezone.utc)
    window_start = window_end - timedelta(days=30)

    _, clusters = run_clustering(
        db=db_session,
        window_start=window_start,
        window_end=window_end,
        max_clusters=20,
        save_to_db=False,
    )

    assert isinstance(clusters, list)
    if clusters:
        top = clusters[0]
        assert top.count > 0
        assert top.fingerprint is not None
        assert top.importance_score >= 0


def test_explain_window(db_session):
    from datetime import timedelta
    from src.core.explain.summarizer import explain_window

    window_end = datetime.now(tz=timezone.utc)
    window_start = window_end - timedelta(days=30)

    result = explain_window(
        db=db_session,
        window_start=window_start,
        window_end=window_end,
        no_llm=True,
    )

    assert result.summary_text
    assert result.confidence in ("low", "medium", "medium-high", "high")
    assert isinstance(result.evidence_items, list)
    assert isinstance(result.services_affected, list)


def test_ask_question(db_session):
    from datetime import timedelta
    from src.core.retrieval.question_router import answer_question

    window_end = datetime.now(tz=timezone.utc)
    window_start = window_end - timedelta(days=30)

    result = answer_question(
        db=db_session,
        question="why did webhook fail?",
        window_start=window_start,
        window_end=window_end,
    )

    assert result.question == "why did webhook fail?"
    assert result.answer_text
    assert isinstance(result.evidence_items, list)
