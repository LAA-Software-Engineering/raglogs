"""
Integration test for the CloudWatch adapter — full ingest -> explain flow using moto's
mocked AWS Logs API (no real AWS calls, no localstack container required).

Requires DB_URL to point to a test database, same gate as test_full_flow.py.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)


@pytest.fixture(scope="module")
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
    Base.metadata.create_all(engine)

    with get_db() as db:
        yield db


def test_cloudwatch_ingest_then_explain(db_session):
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")

    from src.adapters.base import SourceSpec, TimeWindow
    from src.core.explain.summarizer import explain_window
    from src.core.ingestion.service import ingest_from_source
    from src.db.models import LogEntry

    with moto.mock_aws():
        client = boto3.client("logs", region_name="us-east-1")
        client.create_log_group(logGroupName="/aws/lambda/my-service")
        client.create_log_stream(logGroupName="/aws/lambda/my-service", logStreamName="stream-1")

        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        client.put_log_events(
            logGroupName="/aws/lambda/my-service",
            logStreamName="stream-1",
            logEvents=[
                {
                    "timestamp": now_ms,
                    "message": (
                        '{"level": "error", "message": '
                        '"Stripe webhook signature verification failed", '
                        '"service": "billing-worker"}'
                    ),
                }
                for _ in range(5)
            ],
        )

        window_end = datetime.now(tz=timezone.utc) + timedelta(minutes=5)
        window_start = window_end - timedelta(hours=1)

        spec = SourceSpec(adapter="cloudwatch", params={"log_group": "/aws/lambda/my-service"})
        job, stats = ingest_from_source(
            db=db_session,
            spec=spec,
            window=TimeWindow(start=window_start, end=window_end),
        )

        assert job.status == "completed"
        assert job.source_adapter == "cloudwatch"
        assert job.source_ref == "/aws/lambda/my-service"
        assert stats.parsed_count == 5
        assert "billing-worker" in stats.services_detected

        # The fixture JSON has no "timestamp" field of its own — this proves the
        # CloudWatch event timestamp -> LogEntry.timestamp fallback actually landed
        # (previously these rows persisted with a null timestamp and were invisible
        # to every windowed query).
        timestamped = (
            db_session.query(LogEntry)
            .filter(LogEntry.ingestion_job_id == job.id, LogEntry.timestamp.isnot(None))
            .count()
        )
        assert timestamped == 5

        result = explain_window(
            db=db_session,
            window_start=window_start,
            window_end=window_end,
            no_llm=True,
            ingestion_job_id=job.id,
        )
        assert result.summary_text
        assert result.total_logs == 5
