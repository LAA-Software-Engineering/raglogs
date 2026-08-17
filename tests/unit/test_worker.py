"""
Tests for src.worker.runner

Uses unittest.mock to avoid requiring a real database. Tests the
claim/dispatch/failure state machine without I/O.
"""
import pytest
from unittest.mock import MagicMock, patch

from src.worker.runner import claim_next_job, process_one


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_db():
    db = MagicMock()
    db.flush = MagicMock()
    db.add = MagicMock()
    db.commit = MagicMock()
    return db


def _mock_worker_job(status="pending", job_type="ingest", payload=None):
    job = MagicMock()
    job.id = "test-uuid-1234"
    job.status = status
    job.job_type = job_type
    job.payload_json = payload or {
        "paths": ["/app/logs"],
        "recursive": False,
        "format": "auto",
        "source_name": None,
        "service": None,
        "env": None,
    }
    job.ingestion_job_id = None
    job.error = None
    job.result_json = None
    job.finished_at = None
    return job


# ── claim_next_job ────────────────────────────────────────────────────────────

class TestClaimNextJob:
    def test_returns_none_when_no_pending_jobs(self):
        db = _mock_db()
        db.execute.return_value.scalar_one_or_none.return_value = None
        result = claim_next_job(db)
        assert result is None

    def test_returns_job_when_pending_exists(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job
        result = claim_next_job(db)
        assert result is job

    def test_sets_status_to_running(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job
        claim_next_job(db)
        assert job.status == "running"

    def test_sets_started_at(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job
        claim_next_job(db)
        assert job.started_at is not None
        assert job.started_at.tzinfo is not None

    def test_calls_flush_after_claiming(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job
        claim_next_job(db)
        db.flush.assert_called_once()


# ── process_one ───────────────────────────────────────────────────────────────

class TestProcessOne:
    def test_returns_false_when_no_jobs(self):
        db = _mock_db()
        db.execute.return_value.scalar_one_or_none.return_value = None
        assert process_one(db) is False

    def test_returns_true_when_job_processed(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job

        mock_stats = MagicMock()
        mock_stats.files_processed = 3
        mock_stats.lines_read = 250
        mock_stats.parsed_count = 248
        mock_stats.error_count = 2
        mock_stats.services_detected = {"billing-worker"}
        mock_stats.duration_seconds = 0.5

        mock_ingestion_job = MagicMock()
        mock_ingestion_job.id = "ingestion-uuid-5678"

        with patch("src.worker.runner.run_ingest_job", return_value={
            "ingestion_job_id": "ingestion-uuid-5678",
            "files_processed": 3,
            "lines_read": 250,
            "parsed_count": 248,
            "error_count": 2,
            "services_detected": ["billing-worker"],
            "duration_seconds": 0.5,
        }):
            result = process_one(db)

        assert result is True

    def test_sets_done_status_on_success(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job

        with patch("src.worker.runner.run_ingest_job", return_value={"ingestion_job_id": "x"}):
            process_one(db)

        assert job.status == "done"

    def test_sets_result_json_on_success(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job
        expected_result = {"ingestion_job_id": "x", "parsed_count": 100}

        with patch("src.worker.runner.run_ingest_job", return_value=expected_result):
            process_one(db)

        assert job.result_json == expected_result

    def test_sets_finished_at_on_success(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job

        with patch("src.worker.runner.run_ingest_job", return_value={"ingestion_job_id": "x"}):
            process_one(db)

        assert job.finished_at is not None

    def test_sets_failed_status_on_exception(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job

        with patch("src.worker.runner.run_ingest_job", side_effect=RuntimeError("disk full")):
            result = process_one(db)

        assert job.status == "failed"
        assert result is True  # processed (even if failed) — don't sleep

    def test_stores_error_message_on_failure(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job

        with patch("src.worker.runner.run_ingest_job", side_effect=RuntimeError("disk full")):
            process_one(db)

        assert "disk full" in job.error

    def test_sets_finished_at_on_failure(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job

        with patch("src.worker.runner.run_ingest_job", side_effect=ValueError("bad input")):
            process_one(db)

        assert job.finished_at is not None

    def test_non_file_adapter_dispatches_to_ingest_from_source(self):
        db = _mock_db()
        job = _mock_worker_job(payload={
            "adapter": "cloudwatch",
            "params": {"log_group": "/aws/lambda/x"},
            "service": None,
            "env": None,
            "source_name": None,
            "format": "auto",
        })
        db.execute.return_value.scalar_one_or_none.return_value = job

        mock_ingestion_job = MagicMock()
        mock_ingestion_job.id = "ingestion-uuid-cw"
        mock_stats = MagicMock()
        mock_stats.files_processed = 1
        mock_stats.lines_read = 10
        mock_stats.parsed_count = 10
        mock_stats.error_count = 0
        mock_stats.services_detected = set()
        mock_stats.duration_seconds = 0.1

        with patch(
            "src.core.ingestion.service.ingest_from_source",
            return_value=(mock_ingestion_job, mock_stats),
        ) as mock_ingest:
            result = process_one(db)

        assert result is True
        assert job.status == "done"
        mock_ingest.assert_called_once()

    def test_non_file_adapter_resolves_since_only_window(self):
        """
        Regression test: the worker used to build a window only when BOTH
        window_start and window_end were present, so a single bound (e.g. "since")
        was silently ignored and the ingest defaulted to the last 1h instead.
        """
        db = _mock_db()
        job = _mock_worker_job(payload={
            "adapter": "cloudwatch",
            "params": {"log_group": "/aws/lambda/x"},
            "service": None,
            "env": None,
            "source_name": None,
            "format": "auto",
            "since": "2h",
        })
        db.execute.return_value.scalar_one_or_none.return_value = job

        mock_ingestion_job = MagicMock()
        mock_stats = MagicMock()

        with patch(
            "src.core.ingestion.service.ingest_from_source",
            return_value=(mock_ingestion_job, mock_stats),
        ) as mock_ingest:
            process_one(db)

        window = mock_ingest.call_args.kwargs["window"]
        assert window is not None
        assert (window.end - window.start).total_seconds() == pytest.approx(2 * 3600, abs=5)

    def test_non_file_adapter_resolves_from_time_only_window(self):
        """
        Regression test: a naive ISO datetime string must get UTC attached (the CLI's
        resolve_window already does this) — otherwise window.start.timestamp() uses the
        worker process's local timezone and the CloudWatch query window is shifted.
        """
        db = _mock_db()
        job = _mock_worker_job(payload={
            "adapter": "cloudwatch",
            "params": {"log_group": "/aws/lambda/x"},
            "service": None,
            "env": None,
            "source_name": None,
            "format": "auto",
            "from_time": "2026-01-01T00:00:00",  # naive — no tzinfo
        })
        db.execute.return_value.scalar_one_or_none.return_value = job

        mock_ingestion_job = MagicMock()
        mock_stats = MagicMock()

        with patch(
            "src.core.ingestion.service.ingest_from_source",
            return_value=(mock_ingestion_job, mock_stats),
        ) as mock_ingest:
            process_one(db)

        window = mock_ingest.call_args.kwargs["window"]
        assert window.start.tzinfo is not None

    def test_unknown_resume_job_fails_the_worker_job(self):
        """
        Regression test: an unresolvable resume_ingestion_job_id used to be silently
        swallowed (resume_cursors stayed None), so the run re-read the entire window
        instead of failing loudly.
        """
        db = _mock_db()
        job = _mock_worker_job(payload={
            "adapter": "cloudwatch",
            "params": {"log_group": "/aws/lambda/x"},
            "service": None,
            "env": None,
            "source_name": None,
            "format": "auto",
            "resume_ingestion_job_id": "00000000-0000-0000-0000-000000000000",
        })
        db.execute.return_value.scalar_one_or_none.return_value = job
        db.query.return_value.filter.return_value.first.return_value = None

        with patch("src.core.ingestion.service.ingest_from_source") as mock_ingest:
            result = process_one(db)

        assert result is True
        assert job.status == "failed"
        assert "00000000-0000-0000-0000-000000000000" in job.error
        mock_ingest.assert_not_called()

    def test_resume_job_in_other_scope_fails_the_worker_job(self):
        """resume_ingestion_job_id must belong to the same isolation scope."""
        db = _mock_db()
        job = _mock_worker_job(payload={
            "adapter": "cloudwatch",
            "params": {"log_group": "/aws/lambda/x"},
            "service": None,
            "env": None,
            "source_name": None,
            "format": "auto",
            "scope": "incident:B",
            "resume_ingestion_job_id": "11111111-1111-1111-1111-111111111111",
        })
        db.execute.return_value.scalar_one_or_none.return_value = job
        prior = MagicMock()
        prior.scope = "incident:A"
        db.query.return_value.filter.return_value.first.return_value = prior

        with patch("src.core.ingestion.service.ingest_from_source") as mock_ingest:
            result = process_one(db)

        assert result is True
        assert job.status == "failed"
        assert "different scope" in job.error
        mock_ingest.assert_not_called()

    def test_unknown_job_type_fails_gracefully(self):
        db = _mock_db()
        job = _mock_worker_job(job_type="unknown_type")
        db.execute.return_value.scalar_one_or_none.return_value = job

        result = process_one(db)

        assert job.status == "failed"
        assert "Unknown job type" in job.error
        assert result is True

    def test_flush_called_after_completion(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job

        with patch("src.worker.runner.run_ingest_job", return_value={"ingestion_job_id": "x"}):
            process_one(db)

        # flush is called twice: once in claim_next_job, once after completion
        assert db.flush.call_count == 2

    def test_callback_invoked_on_done(self):
        db = _mock_db()
        job = _mock_worker_job(payload={
            "paths": ["/app/logs"],
            "callback_url": "https://hooks.example.com/cb",
            "scope": "default",
        })
        db.execute.return_value.scalar_one_or_none.return_value = job

        with patch("src.worker.runner.run_ingest_job", return_value={
            "ingestion_job_id": "x",
            "parsed_count": 10,
            "error_count": 0,
        }), patch(
            "src.core.ingestion.webhooks.maybe_deliver_ingest_callback",
        ) as mock_deliver:
            process_one(db)

        assert job.status == "done"
        mock_deliver.assert_called_once_with(db, job)

    def test_commits_terminal_status_before_webhook_delivery(self):
        db = _mock_db()
        job = _mock_worker_job(payload={
            "paths": ["/app/logs"],
            "callback_url": "https://hooks.example.com/cb",
        })
        db.execute.return_value.scalar_one_or_none.return_value = job

        order: list[str] = []
        db.commit.side_effect = lambda: order.append("commit")

        with patch(
            "src.worker.runner.run_ingest_job",
            return_value={"ingestion_job_id": "x"},
        ), patch(
            "src.core.ingestion.webhooks.maybe_deliver_ingest_callback",
            side_effect=lambda *_a, **_k: order.append("deliver"),
        ) as mock_deliver:
            process_one(db)

        assert job.status == "done"
        mock_deliver.assert_called_once_with(db, job)
        db.commit.assert_called()
        assert order == ["commit", "deliver"]

    def test_callback_invoked_on_failed(self):
        db = _mock_db()
        job = _mock_worker_job(payload={
            "paths": ["/app/logs"],
            "callback_url": "https://hooks.example.com/cb",
        })
        db.execute.return_value.scalar_one_or_none.return_value = job

        order: list[str] = []
        db.commit.side_effect = lambda: order.append("commit")

        with patch(
            "src.worker.runner.run_ingest_job",
            side_effect=RuntimeError("disk full"),
        ), patch(
            "src.core.ingestion.webhooks.maybe_deliver_ingest_callback",
            side_effect=lambda *_a, **_k: order.append("deliver"),
        ) as mock_deliver:
            result = process_one(db)

        assert result is True
        assert job.status == "failed"
        mock_deliver.assert_called_once_with(db, job)
        assert order == ["commit", "deliver"]

    def test_no_callback_url_still_skips_http_inside_helper(self):
        db = _mock_db()
        job = _mock_worker_job()
        db.execute.return_value.scalar_one_or_none.return_value = job

        with patch("src.worker.runner.run_ingest_job", return_value={"ingestion_job_id": "x"}), \
             patch("src.core.ingestion.webhooks.deliver_callback") as mock_http:
            process_one(db)

        mock_http.assert_not_called()

    def test_webhook_exception_does_not_fail_job(self):
        db = _mock_db()
        job = _mock_worker_job(payload={
            "paths": ["/app/logs"],
            "callback_url": "https://hooks.example.com/cb",
        })
        db.execute.return_value.scalar_one_or_none.return_value = job

        with patch("src.worker.runner.run_ingest_job", return_value={"ingestion_job_id": "x"}), \
             patch(
                 "src.core.ingestion.webhooks._maybe_deliver_ingest_callback",
                 side_effect=RuntimeError("boom"),
             ):
            result = process_one(db)

        assert result is True
        assert job.status == "done"


class TestPurgeJob:
    def test_purge_job_type_dispatches(self):
        db = _mock_db()
        job = _mock_worker_job(job_type="purge", payload={})
        db.execute.return_value.scalar_one_or_none.return_value = job

        with patch(
            "src.core.retention.purge.run_purge_job",
            return_value={"raw": 3, "summary": 0, "embedding": 1, "scopes": ["default"]},
        ) as mock_purge:
            result = process_one(db)

        assert result is True
        assert job.status == "done"
        assert job.result_json["raw"] == 3
        mock_purge.assert_called_once_with(db, job)

    def test_purge_job_skips_ingest_webhook(self):
        db = _mock_db()
        job = _mock_worker_job(job_type="purge", payload={})
        db.execute.return_value.scalar_one_or_none.return_value = job

        with patch(
            "src.core.retention.purge.run_purge_job",
            return_value={"raw": 0, "summary": 0, "embedding": 0, "scopes": []},
        ), patch(
            "src.core.ingestion.webhooks.maybe_deliver_ingest_callback",
        ) as mock_deliver:
            process_one(db)

        mock_deliver.assert_not_called()
        assert job.status == "done"
