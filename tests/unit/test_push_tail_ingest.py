"""Unit tests for push NDJSON ingest, queue backpressure, and tail jobs."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.adapters.base import TimeWindow
from src.api.app import app
from src.core.ingestion.backpressure import ingest_queue_is_full
from src.core.ingestion.push import NdjsonParseError, parse_ndjson_payload
from src.core.ingestion.tail import (
    TailLifecycleError,
    _due_tail_jobs,
    apply_tail_lifecycle,
    consecutive_errors_after_failure,
    persist_cursors,
    tick_one_tail_job,
    tick_window_for_job,
)
from src.db.models import IngestionJob

client = TestClient(app, raise_server_exceptions=False)


def _ctx_db(*, execute_scalar: int = 0, query_result=None):
    mock_db = MagicMock()
    mock_db.__enter__ = MagicMock(return_value=mock_db)
    mock_db.__exit__ = MagicMock(return_value=False)
    mock_db.query.return_value.filter.return_value.first.return_value = query_result
    mock_db.execute.return_value.scalar_one.return_value = execute_scalar
    return mock_db


# ── NDJSON parsing ────────────────────────────────────────────────────────────


class TestParseNdjson:
    def test_raw_and_json_objects(self) -> None:
        body = (
            "plain text line\n"
            '{"message": "boom", "level": "error", "service": "api"}\n'
            '{"raw": "from raw field", "timestamp": "2026-01-01T00:00:00Z"}\n'
            '"quoted string line"\n'
        )
        lines = parse_ndjson_payload(body, max_lines=5000)
        assert lines[0] == "plain text line"
        assert "boom" in lines[1]
        assert "from raw field" in lines[2]
        assert lines[3] == "quoted string line"

    def test_blank_lines_ignored(self) -> None:
        lines = parse_ndjson_payload("\n\na\n  \nb\n", max_lines=10)
        assert lines == ["a", "b"]

    def test_empty_body_raises(self) -> None:
        with pytest.raises(NdjsonParseError, match="empty"):
            parse_ndjson_payload("", max_lines=10)
        with pytest.raises(NdjsonParseError, match="empty"):
            parse_ndjson_payload("  \n\n", max_lines=10)

    def test_over_max_lines_raises(self) -> None:
        body = "a\nb\nc\n"
        with pytest.raises(NdjsonParseError, match="INGEST_PUSH_MAX_LINES"):
            parse_ndjson_payload(body, max_lines=2)


# ── Backpressure helper ───────────────────────────────────────────────────────


class TestQueueBackpressure:
    def test_full_at_max(self) -> None:
        assert ingest_queue_is_full(100, 100) is True
        assert ingest_queue_is_full(101, 100) is True
        assert ingest_queue_is_full(99, 100) is False
        assert ingest_queue_is_full(0, 100) is False


# ── Tail lifecycle state machine ──────────────────────────────────────────────


class TestTailLifecycle:
    def test_pause_resume_stop(self) -> None:
        assert apply_tail_lifecycle("tail", "running", "pause") == "paused"
        assert apply_tail_lifecycle("tail", "paused", "resume") == "running"
        assert apply_tail_lifecycle("tail", "running", "stop") == "stopped"
        assert apply_tail_lifecycle("tail", "paused", "stop") == "stopped"
        assert apply_tail_lifecycle("tail", "stopped", "stop") == "stopped"

    def test_not_tail_is_error(self) -> None:
        with pytest.raises(TailLifecycleError) as exc:
            apply_tail_lifecycle("batch", "running", "pause")
        assert exc.value.code == "not_tail"

    def test_cannot_resume_or_pause_stopped(self) -> None:
        with pytest.raises(TailLifecycleError) as exc:
            apply_tail_lifecycle("tail", "stopped", "resume")
        assert exc.value.code == "conflict"
        with pytest.raises(TailLifecycleError):
            apply_tail_lifecycle("tail", "stopped", "pause")

    def test_idempotent_pause_and_resume(self) -> None:
        assert apply_tail_lifecycle("tail", "paused", "pause") == "paused"
        assert apply_tail_lifecycle("tail", "running", "resume") == "running"


class TestAutoPauseCounter:
    def test_pauses_at_threshold(self) -> None:
        count, pause = consecutive_errors_after_failure(4, 5)
        assert count == 5
        assert pause is True

    def test_does_not_pause_before_threshold(self) -> None:
        count, pause = consecutive_errors_after_failure(0, 5)
        assert count == 1
        assert pause is False


# ── Routes ────────────────────────────────────────────────────────────────────


class TestPushLinesRoute:
    def test_returns_counts_when_persist_mocked(self) -> None:
        mock_job = MagicMock()
        mock_job.id = uuid.uuid4()
        mock_job.status = "completed"
        mock_stats = MagicMock()
        mock_stats.lines_read = 2
        mock_stats.parsed_count = 2
        mock_stats.error_count = 0
        mock_db = _ctx_db()

        with (
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
            patch(
                "src.core.ingestion.service.ingest_push_lines",
                return_value=(mock_job, mock_stats),
            ),
        ):
            resp = client.post(
                "/v1/ingestions/lines",
                content='{"message":"a"}\nplain line\n',
                headers={"Content-Type": "application/x-ndjson"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ingestion_job_id"] == str(mock_job.id)
        assert data["parsed_count"] == 2
        assert data["line_count"] == 2
        assert data["mode"] == "push"

    def test_unversioned_alias_also_works(self) -> None:
        mock_job = MagicMock()
        mock_job.id = uuid.uuid4()
        mock_job.status = "completed"
        mock_stats = MagicMock()
        mock_stats.lines_read = 1
        mock_stats.parsed_count = 1
        mock_stats.error_count = 0
        mock_db = _ctx_db()

        with (
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
            patch(
                "src.core.ingestion.service.ingest_push_lines",
                return_value=(mock_job, mock_stats),
            ),
        ):
            resp = client.post(
                "/ingestions/lines",
                content="hello\n",
                headers={"Content-Type": "application/jsonl"},
            )

        assert resp.status_code == 200
        assert resp.headers.get("deprecation") == "true"

    def test_empty_body_400(self) -> None:
        resp = client.post(
            "/v1/ingestions/lines",
            content="",
            headers={"Content-Type": "application/x-ndjson"},
        )
        assert resp.status_code == 400

    def test_over_max_lines_400(self) -> None:
        from src.config.settings import Settings

        settings = Settings(_env_file=None, ingest_push_max_lines=1)
        with patch("src.config.get_settings", return_value=settings):
            resp = client.post(
                "/v1/ingestions/lines",
                content="a\nb\n",
                headers={"Content-Type": "text/plain"},
            )
        assert resp.status_code == 400
        assert "INGEST_PUSH_MAX_LINES" in resp.json()["detail"]

    def test_get_lines_is_not_invalid_uuid(self) -> None:
        resp = client.get("/ingestions/lines")
        assert resp.status_code == 405
        assert resp.status_code != 422
        assert resp.status_code != 400

        resp_v1 = client.get("/v1/ingestions/lines")
        assert resp_v1.status_code == 405


class TestQueueFull429:
    def test_post_ingestions_429_when_pending_at_max(self) -> None:
        mock_db = _ctx_db(execute_scalar=100)
        with (
            patch("src.adapters.file.adapter.discover_files", return_value=["f.log"]),
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
        ):
            resp = client.post("/v1/ingestions", json={"paths": ["/logs"]})

        assert resp.status_code == 429
        body = resp.json()
        assert body["error_code"] == "INGEST_QUEUE_FULL"
        assert "message" in body
        assert resp.headers.get("retry-after") == "5"

    def test_push_lines_429_when_queue_full(self) -> None:
        mock_db = _ctx_db(execute_scalar=100)
        with (
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
            patch("src.core.ingestion.service.ingest_push_lines") as mock_ingest,
        ):
            resp = client.post(
                "/v1/ingestions/lines",
                content="hello\n",
                headers={"Content-Type": "application/x-ndjson"},
            )

        assert resp.status_code == 429
        assert resp.json()["error_code"] == "INGEST_QUEUE_FULL"
        mock_ingest.assert_not_called()


class TestTailCreateAndLifecycleRoutes:
    def test_create_tail_job(self) -> None:
        mock_db = _ctx_db()
        added: list[object] = []

        def capture_add(obj: object) -> None:
            added.append(obj)
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()  # type: ignore[attr-defined]

        mock_db.add.side_effect = capture_add
        mock_adapter = MagicMock()
        mock_adapter.discover.return_value = [MagicMock(stream_id="/aws/lambda/x")]

        with (
            patch("src.adapters.registry.get_adapter", return_value=mock_adapter),
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
        ):
            resp = client.post(
                "/v1/ingestions",
                json={
                    "adapter": "cloudwatch",
                    "params": {"log_group": "/aws/lambda/x"},
                    "mode": "tail",
                    "paths": [],
                },
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["mode"] == "tail"
        assert data["status"] == "running"
        assert data["worker_job_id"] is None
        assert data["ingestion_job_id"]
        jobs = [o for o in added if isinstance(o, IngestionJob)]
        assert len(jobs) == 1
        assert jobs[0].mode == "tail"
        assert jobs[0].status == "running"

    def test_tail_rejects_file_adapter(self) -> None:
        resp = client.post(
            "/v1/ingestions",
            json={"adapter": "file", "paths": ["/logs"], "mode": "tail"},
        )
        assert resp.status_code == 422

    def test_pause_resume_stop(self) -> None:
        job = MagicMock()
        job.id = uuid.uuid4()
        job.mode = "tail"
        job.status = "running"
        job.finished_at = None
        mock_db = _ctx_db(query_result=job)

        with patch("src.db.session.get_db", side_effect=lambda: mock_db):
            paused = client.post(f"/v1/ingestions/{job.id}:pause")
            assert paused.status_code == 200
            assert paused.json()["status"] == "paused"
            assert job.status == "paused"

            resumed = client.post(f"/v1/ingestions/{job.id}:resume")
            assert resumed.status_code == 200
            assert resumed.json()["status"] == "running"

            stopped = client.post(f"/v1/ingestions/{job.id}:stop")
            assert stopped.status_code == 200
            assert stopped.json()["status"] == "stopped"
            assert job.finished_at is not None

    def test_pause_missing_job_404(self) -> None:
        mock_db = _ctx_db(query_result=None)
        with patch("src.db.session.get_db", side_effect=lambda: mock_db):
            resp = client.post(f"/v1/ingestions/{uuid.uuid4()}:pause")
        assert resp.status_code == 404

    def test_pause_batch_job_409(self) -> None:
        job = MagicMock()
        job.id = uuid.uuid4()
        job.mode = "batch"
        job.status = "completed"
        mock_db = _ctx_db(query_result=job)
        with patch("src.db.session.get_db", side_effect=lambda: mock_db):
            resp = client.post(f"/v1/ingestions/{job.id}:pause")
        assert resp.status_code == 409


class TestTickAutoPause:
    def test_auto_pauses_after_threshold(self) -> None:
        job = MagicMock()
        job.id = uuid.uuid4()
        job.mode = "tail"
        job.status = "running"
        job.consecutive_errors = 4
        job.metadata_json = {"adapter": "cloudwatch", "params": {"log_group": "g"}}
        job.cursor = None
        job.last_polled_at = None
        job.source_adapter = "cloudwatch"
        job.error_message = None

        db = MagicMock()
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)
        with patch(
            "src.core.ingestion.service.ingest_from_source",
            side_effect=RuntimeError("adapter down"),
        ):
            tick_one_tail_job(db, job, now, error_threshold=5)

        assert job.status == "paused"
        assert job.consecutive_errors == 5
        assert "adapter down" in job.error_message
        assert job.last_polled_at == now

    def test_failed_tick_is_not_due_until_poll_interval(self) -> None:
        job = MagicMock()
        job.id = uuid.uuid4()
        job.mode = "tail"
        job.status = "running"
        job.consecutive_errors = 0
        job.metadata_json = {"adapter": "cloudwatch", "params": {"log_group": "g"}}
        job.cursor = None
        job.last_polled_at = None
        job.source_adapter = "cloudwatch"
        job.error_message = None

        db = MagicMock()
        now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
        with patch(
            "src.core.ingestion.service.ingest_from_source",
            side_effect=RuntimeError("boom"),
        ):
            tick_one_tail_job(db, job, now, error_threshold=5)

        assert job.last_polled_at == now
        assert job.consecutive_errors == 1
        assert job.status == "running"

        db.execute.return_value.scalars.return_value.all.return_value = [job]
        assert _due_tail_jobs(db, now + timedelta(seconds=1), poll_interval=30) == []
        assert _due_tail_jobs(db, now + timedelta(seconds=31), poll_interval=30) == [
            job
        ]

        job.status = "paused"
        assert _due_tail_jobs(db, now + timedelta(seconds=31), poll_interval=30) == []

    def test_success_resets_counter(self) -> None:
        job = MagicMock()
        job.id = uuid.uuid4()
        job.mode = "tail"
        job.status = "running"
        job.consecutive_errors = 3
        job.metadata_json = {"adapter": "loki", "params": {"query": '{app="api"}'}}
        job.cursor = "{}"
        job.last_polled_at = None
        job.source_adapter = "loki"
        job.error_message = "old"

        db = MagicMock()
        now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
        mock_stats = MagicMock()
        with patch(
            "src.core.ingestion.service.ingest_from_source",
            return_value=(job, mock_stats),
        ):
            tick_one_tail_job(db, job, now, error_threshold=5)

        assert job.status == "running"
        assert job.consecutive_errors == 0
        assert job.error_message is None
        assert job.last_polled_at == now


class TestTailWindowPagination:
    def test_open_cursor_holds_window_until_exhausted(self) -> None:
        window_start = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 8, 17, 12, 1, tzinfo=timezone.utc)
        now = datetime(2026, 8, 17, 12, 1, 30, tzinfo=timezone.utc)
        job = MagicMock()
        job.id = uuid.uuid4()
        job.mode = "tail"
        job.status = "running"
        job.consecutive_errors = 0
        job.metadata_json = {"adapter": "datadog", "params": {"query": "*"}}
        job.cursor = None
        job.last_polled_at = None
        job.source_adapter = "datadog"
        job.error_message = None

        def ingest_with_open_cursor(
            *, existing_job: MagicMock, **_kwargs: object
        ) -> tuple:
            persist_cursors(existing_job, {"stream-0": "page-2"})
            return existing_job, MagicMock()

        db = MagicMock()
        with (
            patch(
                "src.core.ingestion.service.ingest_from_source",
                side_effect=ingest_with_open_cursor,
            ),
            patch(
                "src.core.ingestion.tail.tick_window_for_job",
                return_value=TimeWindow(start=window_start, end=window_end),
            ),
        ):
            tick_one_tail_job(db, job, now, error_threshold=5)

        assert job.last_polled_at == now
        assert job.metadata_json["tail_window_start"] == window_start.isoformat()
        assert job.metadata_json["tail_window_end"] == window_end.isoformat()

        held = tick_window_for_job(job, now + timedelta(minutes=5))
        assert held.start == window_start
        assert held.end == window_end

        later = now + timedelta(seconds=30)

        def ingest_exhausted(*, existing_job: MagicMock, **_kwargs: object) -> tuple:
            persist_cursors(existing_job, {"stream-0": None})
            return existing_job, MagicMock()

        with patch(
            "src.core.ingestion.service.ingest_from_source",
            side_effect=ingest_exhausted,
        ):
            tick_one_tail_job(db, job, later, error_threshold=5)

        assert job.last_polled_at == later
        assert "tail_window_start" not in job.metadata_json
        assert "tail_window_end" not in job.metadata_json

    def test_failed_continuation_keeps_held_window(self) -> None:
        window_start = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 8, 17, 12, 1, tzinfo=timezone.utc)
        hold_now = datetime(2026, 8, 17, 12, 1, 30, tzinfo=timezone.utc)
        job = MagicMock()
        job.id = uuid.uuid4()
        job.mode = "tail"
        job.status = "running"
        job.consecutive_errors = 0
        job.metadata_json = {"adapter": "datadog", "params": {"query": "*"}}
        job.cursor = None
        job.last_polled_at = None
        job.source_adapter = "datadog"
        job.error_message = None

        def ingest_with_open_cursor(
            *, existing_job: MagicMock, **_kwargs: object
        ) -> tuple:
            persist_cursors(existing_job, {"stream-0": "page-2"})
            return existing_job, MagicMock()

        db = MagicMock()
        with (
            patch(
                "src.core.ingestion.service.ingest_from_source",
                side_effect=ingest_with_open_cursor,
            ),
            patch(
                "src.core.ingestion.tail.tick_window_for_job",
                return_value=TimeWindow(start=window_start, end=window_end),
            ),
        ):
            tick_one_tail_job(db, job, hold_now, error_threshold=5)

        fail_now = hold_now + timedelta(minutes=5)
        with patch(
            "src.core.ingestion.service.ingest_from_source",
            side_effect=RuntimeError("page failed"),
        ):
            tick_one_tail_job(db, job, fail_now, error_threshold=5)

        assert job.last_polled_at == fail_now
        assert job.status == "running"
        assert job.consecutive_errors == 1
        db.execute.return_value.scalars.return_value.all.return_value = [job]
        assert (
            _due_tail_jobs(db, fail_now + timedelta(seconds=1), poll_interval=30) == []
        )

        later = fail_now + timedelta(minutes=10)
        held = tick_window_for_job(job, later)
        assert held.start == window_start
        assert held.end == window_end
        assert held != TimeWindow(start=fail_now, end=later)
