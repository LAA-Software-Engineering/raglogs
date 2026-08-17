"""Content-dedup integration test. Skipped without Postgres (CI has no DB)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)


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
    Base.metadata.create_all(engine)

    with get_db() as db:
        yield db


def test_reingest_identical_lines_keeps_single_row(db_session, tmp_path: Path) -> None:
    from sqlalchemy import func, select

    from src.core.ingestion.service import ingest_files
    from src.db.models import LogEntry

    log_file = tmp_path / "dup.log"
    log_file.write_text(
        '{"timestamp": "2026-01-01T00:00:00Z", "message": "boom", "level": "error", "service": "api"}\n'
    )

    ingest_files(db=db_session, paths=[str(log_file)])
    ingest_files(db=db_session, paths=[str(log_file)])
    db_session.flush()

    count = db_session.execute(select(func.count()).select_from(LogEntry)).scalar_one()
    assert count == 1
