"""
Tests for src.core.retrieval.question_router query construction.

These inspect the compiled SQL of the Select objects passed to db.execute()
rather than hitting a real database — unit tests must not require a DB.
"""
import uuid
from unittest.mock import MagicMock

from src.core.retrieval.question_router import fetch_fallback_clusters, search_logs


def _mock_db_returning(rows=None):
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = rows or []
    return db


def _where_clause(mock_db) -> str:
    query = mock_db.execute.call_args[0][0]
    return str(query.whereclause)


class TestSearchLogsIngestionScoping:
    def test_filters_by_ingestion_job_id_when_given(self):
        db = _mock_db_returning()
        job_id = uuid.uuid4()

        search_logs(db, ["error"], None, None, None, None, ingestion_job_id=job_id)

        sql = _where_clause(db)
        assert "ingestion_job_id" in sql

    def test_no_ingestion_filter_when_omitted(self):
        db = _mock_db_returning()

        search_logs(db, ["error"], None, None, None, None)

        sql = _where_clause(db)
        assert "ingestion_job_id" not in sql


class TestFetchFallbackClustersIngestionScoping:
    def test_filters_by_ingestion_job_id_when_given(self):
        from datetime import datetime, timezone

        db = _mock_db_returning()
        job_id = uuid.uuid4()
        window_start = datetime(2026, 8, 16, 5, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 8, 16, 7, 0, tzinfo=timezone.utc)

        fetch_fallback_clusters(db, window_start, window_end, None, None, ingestion_job_id=job_id)

        sql = _where_clause(db)
        assert "ingestion_job_id" in sql

    def test_no_ingestion_filter_when_omitted(self):
        from datetime import datetime, timezone

        db = _mock_db_returning()
        window_start = datetime(2026, 8, 16, 5, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 8, 16, 7, 0, tzinfo=timezone.utc)

        fetch_fallback_clusters(db, window_start, window_end, None, None)

        sql = _where_clause(db)
        assert "ingestion_job_id" not in sql
