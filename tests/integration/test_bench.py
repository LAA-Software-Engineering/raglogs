"""Smoke test for the DB-backed benchmark runner against real Postgres.

Exercises run_benchmark (ingest -> explain_window -> BenchResult) on every CI
run so the runtime path can't silently break between scheduled bench jobs.
Asserts only structural results — never wall-clock time, which is
runner-variance-sensitive.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)


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


def test_run_benchmark_smoke(db_session, tmp_path):
    from src.db.session import get_engine
    from src.perf.bench import run_benchmark

    result = run_benchmark(db_session, get_engine(), n_lines=200, tmp_dir=tmp_path)

    # Structural assertions only — timing is runner-dependent.
    assert result.n_lines == 200
    assert result.ingested == 200
    assert result.total_logs > 0
    assert result.ingest_queries > 0
    assert result.explain_queries > 0
    # Sanity: both phases recorded a non-negative wall time.
    assert result.ingest_seconds >= 0
    assert result.explain_seconds >= 0
