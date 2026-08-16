"""
Tests for FastAPI routes using TestClient.

All route modules use lazy imports inside handler functions, so we patch
at the source module level (src.db.session, src.core.*, etc.) rather than
trying to patch the route module's namespace.

Tests: async ingestion flow, explain cache hit/miss, ingestion_job_id
validation, timeline/compare query routes, health queue depth, and 4xx cases.
"""
import uuid
import pytest
from datetime import datetime, timezone
from contextlib import contextmanager
from unittest.mock import MagicMock, patch, call
from fastapi.testclient import TestClient

from src.api.app import app

client = TestClient(app, raise_server_exceptions=False)


# ── Shared mock helpers ───────────────────────────────────────────────────────

_UNSET = object()


def _ctx_db(query_result=_UNSET, execute_scalar=_UNSET):
    """Build a context-manager mock for a single get_db() call."""
    mock_db = MagicMock()
    mock_db.__enter__ = MagicMock(return_value=mock_db)
    mock_db.__exit__ = MagicMock(return_value=False)
    # Always configure .first() — None means "not found", a value means "found"
    mock_db.query.return_value.filter.return_value.first.return_value = (
        None if query_result is _UNSET else query_result
    )
    if execute_scalar is not _UNSET:
        mock_db.execute.return_value.scalar_one.return_value = execute_scalar
    return mock_db


def _patch_get_db(query_result=_UNSET, execute_scalar=_UNSET):
    """
    Patch src.db.session.get_db as a side_effect factory.
    Each call to get_db() returns a fresh context-manager mock — necessary
    because routes may call get_db() more than once per request.
    """
    def factory():
        return _ctx_db(query_result=query_result, execute_scalar=execute_scalar)
    return patch("src.db.session.get_db", side_effect=factory)


def _mock_worker_job(status="pending", ingestion_job_id=None, error=None, result=None):
    job = MagicMock()
    job.id = uuid.uuid4()
    job.status = status
    job.ingestion_job_id = uuid.UUID(ingestion_job_id) if ingestion_job_id else None
    job.error = error
    job.result_json = result
    job.created_at = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)
    job.started_at = None
    job.finished_at = None
    return job


# ── Health ────────────────────────────────────────────────────────────────────

class TestHealth:
    @pytest.fixture(autouse=True)
    def _clear_adapter_health_cache(self):
        from src.api.routes.health import _adapter_health_cache
        _adapter_health_cache.clear()
        yield
        _adapter_health_cache.clear()

    def test_health_ok_when_db_connected(self):
        with patch("src.db.session.check_connection", return_value=True), \
             _patch_get_db(execute_scalar=3):
            resp = client.get("/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["db"] == "connected"

    def test_health_degraded_when_db_disconnected(self):
        with patch("src.db.session.check_connection", return_value=False):
            resp = client.get("/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["worker_queue_depth"] is None

    def test_health_includes_worker_queue_depth(self):
        with patch("src.db.session.check_connection", return_value=True), \
             _patch_get_db(execute_scalar=7):
            resp = client.get("/health")

        assert resp.json()["worker_queue_depth"] == 7

    def test_health_includes_file_adapter_ok(self):
        with patch("src.db.session.check_connection", return_value=True), \
             _patch_get_db(execute_scalar=3):
            resp = client.get("/health")

        assert resp.json()["adapters"]["file"] == "ok"

    def test_health_reports_cloudwatch_unavailable_without_credentials(self):
        from src.core.errors import AdapterUnavailableError

        with patch("src.db.session.check_connection", return_value=True), \
             _patch_get_db(execute_scalar=3), \
             patch(
                 "src.adapters.cloudwatch.adapter.CloudWatchSourceAdapter.check_available",
                 side_effect=AdapterUnavailableError("no AWS credentials resolved via the default credential chain"),
             ):
            resp = client.get("/health")

        assert resp.json()["adapters"]["cloudwatch"].startswith("unavailable:")

    def test_health_reports_datadog_unavailable_without_keys(self):
        with patch("src.db.session.check_connection", return_value=True), \
             _patch_get_db(execute_scalar=3):
            resp = client.get("/health")

        assert resp.json()["adapters"]["datadog"].startswith("unavailable:")


# ── POST /ingestions ──────────────────────────────────────────────────────────

class TestCreateIngestion:
    def test_returns_202_with_worker_job_id(self):
        wj_id = str(uuid.uuid4())
        mock_db = _ctx_db()
        added_jobs = []

        def capture_add(obj):
            added_jobs.append(obj)
            obj.id = uuid.UUID(wj_id)

        mock_db.add.side_effect = capture_add

        with patch("src.adapters.file.adapter.discover_files", return_value=["f.log"]), \
             patch("src.db.session.get_db", side_effect=lambda: mock_db):
            resp = client.post("/ingestions", json={"paths": ["/logs"]})

        assert resp.status_code == 202
        data = resp.json()
        assert "worker_job_id" in data
        assert data["status"] == "pending"

    def test_400_when_no_files_found(self):
        with patch("src.adapters.file.adapter.discover_files", return_value=[]):
            resp = client.post("/ingestions", json={"paths": ["/nonexistent"]})

        assert resp.status_code == 400
        assert "No log files" in resp.json()["detail"]

    def test_paths_required(self):
        resp = client.post("/ingestions", json={})
        assert resp.status_code == 422  # FastAPI validation

    def test_400_when_adapter_unavailable(self):
        from src.core.errors import AdapterUnavailableError

        with patch(
            "src.adapters.registry.get_adapter",
            side_effect=AdapterUnavailableError("no AWS credentials resolved"),
        ):
            resp = client.post("/ingestions", json={
                "paths": [],
                "adapter": "cloudwatch",
                "params": {"log_group": "/aws/lambda/x"},
            })

        assert resp.status_code == 400
        assert resp.json()["detail"]["error_code"] == "ADAPTER_UNAVAILABLE"

    def test_400_when_cloudwatch_params_missing_log_group(self):
        """
        Exercises the real registry + CloudWatchSourceAdapter.discover() (no mocking) —
        params validation for cloudwatch is local-only, no AWS credentials needed.
        """
        resp = client.post("/ingestions", json={
            "paths": [],
            "adapter": "cloudwatch",
            "params": {},
        })

        assert resp.status_code == 400
        assert resp.json()["detail"]["error_code"] == "ADAPTER_UNAVAILABLE"

    def test_400_when_since_unparseable(self):
        """
        Regression test: an invalid `since` used to be stored as-is and only fail once
        the worker picked up the job (202 now, 400 later, async). It should fail fast
        at enqueue time instead, same as POST /query/explain does.
        """
        resp = client.post("/ingestions", json={
            "paths": [],
            "adapter": "cloudwatch",
            "params": {"log_group": "/aws/lambda/x"},
            "since": "not-a-duration",
        })

        assert resp.status_code == 400

    def test_422_when_paths_omitted_for_file_adapter(self):
        resp = client.post("/ingestions", json={"adapter": "file"})
        assert resp.status_code == 422

    def test_202_for_non_file_adapter_when_available(self):
        wj_id = str(uuid.uuid4())
        mock_db = _ctx_db()

        def capture_add(obj):
            obj.id = uuid.UUID(wj_id)

        mock_db.add.side_effect = capture_add

        mock_adapter = MagicMock()
        mock_adapter.discover.return_value = [MagicMock(stream_id="/aws/lambda/x")]

        with patch("src.adapters.registry.get_adapter", return_value=mock_adapter), \
             patch("src.db.session.get_db", side_effect=lambda: mock_db):
            resp = client.post("/ingestions", json={
                "paths": [],
                "adapter": "cloudwatch",
                "params": {"log_group": "/aws/lambda/x"},
            })

        assert resp.status_code == 202
        assert resp.json()["status"] == "pending"

    def test_202_for_datadog_adapter_with_default_query(self):
        """
        Datadog discover() is local-only and defaults query to '*', so enqueue
        should succeed without keys or an explicit query param.
        """
        wj_id = str(uuid.uuid4())
        mock_db = _ctx_db()

        def capture_add(obj):
            obj.id = uuid.UUID(wj_id)

        mock_db.add.side_effect = capture_add

        with patch("src.db.session.get_db", side_effect=lambda: mock_db):
            resp = client.post("/ingestions", json={
                "paths": [],
                "adapter": "datadog",
                "params": {},
                "since": "1h",
            })

        assert resp.status_code == 202
        assert resp.json()["status"] == "pending"


# ── GET /ingestions/jobs/{id} ─────────────────────────────────────────────────

class TestGetWorkerJobStatus:
    def test_returns_pending_status(self):
        job = _mock_worker_job(status="pending")
        with _patch_get_db(query_result=job):
            resp = client.get(f"/ingestions/jobs/{job.id}")

        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"

    def test_done_status_includes_ingestion_job_id(self):
        ij_id = str(uuid.uuid4())
        job = _mock_worker_job(
            status="done",
            ingestion_job_id=ij_id,
            result={"ingestion_job_id": ij_id, "parsed_count": 248},
        )
        with _patch_get_db(query_result=job):
            resp = client.get(f"/ingestions/jobs/{job.id}")

        data = resp.json()
        assert data["status"] == "done"
        assert data["ingestion_job_id"] == ij_id

    def test_failed_status_includes_error(self):
        job = _mock_worker_job(status="failed", error="disk full")
        with _patch_get_db(query_result=job):
            resp = client.get(f"/ingestions/jobs/{job.id}")

        data = resp.json()
        assert data["status"] == "failed"
        assert data["error"] == "disk full"

    def test_404_when_not_found(self):
        with _patch_get_db(query_result=None):
            resp = client.get(f"/ingestions/jobs/{uuid.uuid4()}")

        assert resp.status_code == 404

    def test_400_on_invalid_uuid(self):
        resp = client.get("/ingestions/jobs/not-a-uuid")
        assert resp.status_code == 400


# ── GET /ingestions (list) ────────────────────────────────────────────────────

class TestListIngestions:
    def _ctx_db_with_rows(self, rows):
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.execute.return_value.all.return_value = rows
        return mock_db

    def test_returns_list_of_summaries(self):
        job = MagicMock()
        job.id = uuid.uuid4()
        job.parsed_count = 404
        job.finished_at = datetime(2026, 8, 16, 5, 59, 0, tzinfo=timezone.utc)
        mock_db = self._ctx_db_with_rows([(job, "sample_incident")])

        with patch("src.db.session.get_db", side_effect=lambda: mock_db):
            resp = client.get("/ingestions")

        assert resp.status_code == 200
        ingestions = resp.json()["ingestions"]
        assert len(ingestions) == 1
        assert ingestions[0]["ingestion_job_id"] == str(job.id)
        assert ingestions[0]["source_name"] == "sample_incident"
        assert ingestions[0]["parsed_count"] == 404

    def test_returns_empty_list_when_no_ingestions(self):
        mock_db = self._ctx_db_with_rows([])

        with patch("src.db.session.get_db", side_effect=lambda: mock_db):
            resp = client.get("/ingestions")

        assert resp.status_code == 200
        assert resp.json()["ingestions"] == []


# ── GET /ingestions/latest ────────────────────────────────────────────────────

class TestLatestIngestion:
    def _ctx_db_with_scalar_one_or_none(self, job):
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.execute.return_value.scalar_one_or_none.return_value = job
        return mock_db

    def test_returns_job_id_when_completed_ingestion_exists(self):
        job = MagicMock()
        job.id = uuid.uuid4()
        mock_db = self._ctx_db_with_scalar_one_or_none(job)

        with patch("src.db.session.get_db", side_effect=lambda: mock_db):
            resp = client.get("/ingestions/latest")

        assert resp.status_code == 200
        assert resp.json()["ingestion_job_id"] == str(job.id)

    def test_returns_null_when_no_completed_ingestion(self):
        mock_db = self._ctx_db_with_scalar_one_or_none(None)

        with patch("src.db.session.get_db", side_effect=lambda: mock_db):
            resp = client.get("/ingestions/latest")

        assert resp.status_code == 200
        assert resp.json()["ingestion_job_id"] is None

    def test_not_swallowed_by_ingestion_job_id_path_param(self):
        """
        /ingestions/latest must be registered before /{ingestion_job_id},
        otherwise "latest" is parsed as an invalid UUID and 400s.
        """
        mock_db = self._ctx_db_with_scalar_one_or_none(None)

        with patch("src.db.session.get_db", side_effect=lambda: mock_db):
            resp = client.get("/ingestions/latest")

        assert resp.status_code != 400


# ── GET / (web UI shell) ──────────────────────────────────────────────────────

class TestUIShell:
    def test_index_returns_200_html(self):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "raglogs" in resp.text

    def test_index_includes_all_four_tabs(self):
        resp = client.get("/")
        for tab in ("Explain", "Timeline", "Compare", "Ask"):
            assert tab in resp.text

    def test_static_css_served(self):
        resp = client.get("/static/css/app.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]

    def test_static_js_served(self):
        resp = client.get("/static/js/app.js")
        assert resp.status_code == 200


# ── POST /query/explain ───────────────────────────────────────────────────────

class TestExplainEndpoint:
    def _mock_result(self):
        result = MagicMock()
        result.window_start = datetime(2026, 3, 12, 13, 0, 0, tzinfo=timezone.utc)
        result.window_end = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)
        result.summary_text = "Incident summary\n\nWindow: ..."
        result.confidence = "high"
        result.mode = "rules"
        result.total_logs = 404
        result.services_affected = ["api", "billing-worker"]
        result.primary_cluster = {"message": "Stripe error", "count": 184}
        result.secondary_clusters = []
        result.trigger_candidates = []
        result.evidence_items = ["184 similar failures in billing-worker"]
        return result

    def test_returns_200_with_summary(self):
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch("src.core.explain.summarizer.explain_window", return_value=self._mock_result()), \
             patch("src.api.routes.explain._load_from_cache", return_value=None), \
             patch("src.api.routes.explain._save_to_cache"):
            resp = client.post("/query/explain", json={"since": "1h"})

        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert data["confidence"] == "high"
        assert data["cached"] is False

    def test_cache_hit_returns_cached_true(self):
        cached_payload = {
            "window": {"start": "2026-03-12T13:00:00+00:00", "end": "2026-03-12T14:00:00+00:00"},
            "summary": "Incident summary...",
            "confidence": "high",
            "mode": "rules",
            "total_logs": 404,
            "services_affected": ["api"],
            "primary_cluster": None,
            "secondary_clusters": [],
            "trigger_candidates": [],
            "evidence": [],
            "cached": True,
        }
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch("src.api.routes.explain._load_from_cache", return_value=cached_payload):
            resp = client.post("/query/explain", json={"since": "1h"})

        assert resp.status_code == 200
        assert resp.json()["cached"] is True

    def test_force_refresh_bypasses_cache(self):
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch("src.core.explain.summarizer.explain_window", return_value=self._mock_result()) as mock_explain, \
             patch("src.api.routes.explain._load_from_cache") as mock_load, \
             patch("src.api.routes.explain._save_to_cache"):
            resp = client.post("/query/explain", json={"since": "1h", "force_refresh": True})

        mock_load.assert_not_called()
        mock_explain.assert_called_once()
        assert resp.status_code == 200

    def test_invalid_ingestion_job_id_returns_400(self):
        resp = client.post("/query/explain", json={
            "since": "1h",
            "ingestion_job_id": "not-a-uuid",
        })
        assert resp.status_code == 400
        assert "ingestion_job_id" in resp.json()["detail"]

    def test_valid_ingestion_job_id_passed_to_explain_window(self):
        ij_id = str(uuid.uuid4())
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch("src.core.explain.summarizer.explain_window", return_value=self._mock_result()) as mock_explain, \
             patch("src.api.routes.explain._load_from_cache", return_value=None), \
             patch("src.api.routes.explain._save_to_cache"):
            resp = client.post("/query/explain", json={"since": "1h", "ingestion_job_id": ij_id})

        assert resp.status_code == 200
        call_kwargs = mock_explain.call_args.kwargs
        assert str(call_kwargs["ingestion_job_id"]) == ij_id

    def test_missing_time_params_returns_400(self):
        resp = client.post("/query/explain", json={})
        assert resp.status_code == 400


# ── POST /query/ask ────────────────────────────────────────────────────────────

class TestAskEndpoint:
    def _mock_result(self):
        from src.core.retrieval.question_router import AskResult

        return AskResult(
            question="why did checkout fail?",
            answer_text="Stripe signature verification failed for /webhooks/stripe.",
            evidence_items=["184 events: 'Stripe signature verification failed' in billing-worker"],
            clusters_used=[{"message": "Stripe signature verification failed", "count": 184, "services": ["billing-worker"]}],
            total_matches=184,
        )

    def test_returns_200_with_answer(self):
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch("src.core.retrieval.question_router.answer_question", return_value=self._mock_result()):
            resp = client.post("/query/ask", json={"question": "why did checkout fail?", "since": "1h"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == "Stripe signature verification failed for /webhooks/stripe."
        assert data["total_matches"] == 184

    def test_ingestion_job_id_passed_through(self):
        """
        Regression test: the ingestion picker in the web UI sends ingestion_job_id
        on every request, including Ask. AskRequest previously had no such field,
        so pydantic silently dropped it and Ask always searched across every
        ingestion regardless of what the user selected.
        """
        ij_id = str(uuid.uuid4())
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch("src.core.retrieval.question_router.answer_question", return_value=self._mock_result()) as mock_answer:
            resp = client.post("/query/ask", json={
                "question": "why did checkout fail?",
                "since": "1h",
                "ingestion_job_id": ij_id,
            })

        assert resp.status_code == 200
        call_kwargs = mock_answer.call_args.kwargs
        assert str(call_kwargs["ingestion_job_id"]) == ij_id

    def test_invalid_ingestion_job_id_returns_400(self):
        resp = client.post("/query/ask", json={
            "question": "why did checkout fail?",
            "since": "1h",
            "ingestion_job_id": "not-a-uuid",
        })
        assert resp.status_code == 400
        assert "ingestion_job_id" in resp.json()["detail"]

    def test_question_required(self):
        resp = client.post("/query/ask", json={})
        assert resp.status_code == 422


# ── POST /query/clusters ──────────────────────────────────────────────────────

class TestClustersEndpoint:
    def test_returns_clusters_list(self):
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch("src.core.clustering.clusterer.run_clustering", return_value=(None, [])):
            resp = client.post("/query/clusters", json={"since": "1h"})

        assert resp.status_code == 200
        assert "clusters" in resp.json()

    def test_invalid_ingestion_job_id_returns_400(self):
        resp = client.post("/query/clusters", json={
            "since": "1h",
            "ingestion_job_id": "bad-uuid",
        })
        assert resp.status_code == 400


# ── POST /query/timeline ─────────────────────────────────────────────────────

class TestTimelineEndpoint:
    def test_returns_events_list(self):
        from src.core.timeline.builder import TimelineEvent

        events = [
            TimelineEvent(
                timestamp=datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc),
                category="deploy",
                label="deploy",
                description="Deploy completed",
                count=None,
                services=["deployment-controller"],
                duration_minutes=None,
            ),
        ]
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch("src.core.clustering.clusterer.run_clustering", return_value=(None, [])), \
             patch("src.core.explain.evidence.assemble_evidence", return_value=MagicMock()), \
             patch("src.core.timeline.builder.build_timeline", return_value=events):
            resp = client.post("/query/timeline", json={"since": "1h"})

        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert len(data["events"]) == 1
        assert data["events"][0]["category"] == "deploy"
        assert data["events"][0]["label"] == "deploy"

    def test_format_text_includes_rendered_field(self):
        from src.core.timeline.builder import TimelineEvent

        events = [
            TimelineEvent(
                timestamp=datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc),
                category="error",
                label="error ↑",
                description="Something failed",
                count=5,
                services=["api"],
                duration_minutes=3,
            ),
        ]
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch("src.core.clustering.clusterer.run_clustering", return_value=(None, [])), \
             patch("src.core.explain.evidence.assemble_evidence", return_value=MagicMock()), \
             patch("src.core.timeline.builder.build_timeline", return_value=events):
            resp = client.post("/query/timeline", json={"since": "1h", "format": "text"})

        assert resp.status_code == 200
        assert "text" in resp.json()
        assert "Incident timeline" in resp.json()["text"]

    def test_invalid_ingestion_job_id_returns_400(self):
        resp = client.post("/query/timeline", json={
            "since": "1h",
            "ingestion_job_id": "not-a-uuid",
        })
        assert resp.status_code == 400


# ── POST /query/compare ───────────────────────────────────────────────────────

class TestCompareEndpoint:
    def _mock_compare_result(self):
        from src.core.compare.differ import ClusterDiff, CompareResult, TriggerDiff

        return CompareResult(
            window_a_start=datetime(2026, 3, 16, 15, 17, 42, tzinfo=timezone.utc),
            window_a_end=datetime(2026, 3, 16, 15, 47, 42, tzinfo=timezone.utc),
            window_b_start=datetime(2026, 3, 15, 15, 17, 42, tzinfo=timezone.utc),
            window_b_end=datetime(2026, 3, 15, 15, 47, 42, tzinfo=timezone.utc),
            new_clusters=[
                ClusterDiff(
                    fingerprint="fp1",
                    message="New error",
                    services=["api"],
                    count_a=10,
                    count_b=None,
                ),
            ],
            new_triggers=[
                TriggerDiff(message="Deploy done", service="deploy", only_in="a"),
            ],
        )

    def test_since_baseline_returns_json(self):
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch("src.core.clustering.clusterer.run_clustering", return_value=(None, [])), \
             patch("src.core.explain.evidence.assemble_evidence", return_value=MagicMock()), \
             patch("src.core.compare.differ.compare_windows", return_value=self._mock_compare_result()):
            resp = client.post(
                "/query/compare",
                json={"since": "30m", "baseline": "24h"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_changes"] is True
        assert len(data["new_clusters"]) == 1
        assert data["new_clusters"][0]["message"] == "New error"
        assert data["new_triggers"][0]["service"] == "deploy"
        assert "only_in" in data["new_triggers"][0]

    def test_missing_window_params_returns_400(self):
        resp = client.post("/query/compare", json={})
        assert resp.status_code == 400

    def test_format_text_includes_rendered_field(self):
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch("src.core.clustering.clusterer.run_clustering", return_value=(None, [])), \
             patch("src.core.explain.evidence.assemble_evidence", return_value=MagicMock()), \
             patch("src.core.compare.differ.compare_windows", return_value=self._mock_compare_result()):
            resp = client.post(
                "/query/compare",
                json={"since": "30m", "baseline": "24h", "format": "text"},
            )

        assert resp.status_code == 200
        assert "text" in resp.json()
        assert "Incident comparison" in resp.json()["text"]

    def test_invalid_ingestion_job_id_returns_400(self):
        resp = client.post("/query/compare", json={
            "since": "30m",
            "baseline": "24h",
            "ingestion_job_id": "bad",
        })
        assert resp.status_code == 400


# ── Cache key consistency ─────────────────────────────────────────────────────

class TestCacheKey:
    def test_same_params_same_key(self):
        from src.api.routes.explain import _cache_key
        ws = datetime(2026, 3, 12, 13, 0, 0, tzinfo=timezone.utc)
        we = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)
        assert _cache_key(ws, we, "api", "prod", "abc") == _cache_key(ws, we, "api", "prod", "abc")

    def test_different_service_different_key(self):
        from src.api.routes.explain import _cache_key
        ws = datetime(2026, 3, 12, 13, 0, 0, tzinfo=timezone.utc)
        we = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)
        assert _cache_key(ws, we, "api", None, None) != _cache_key(ws, we, "worker", None, None)

    def test_different_ingestion_job_id_different_key(self):
        from src.api.routes.explain import _cache_key
        ws = datetime(2026, 3, 12, 13, 0, 0, tzinfo=timezone.utc)
        we = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)
        assert _cache_key(ws, we, None, None, "a") != _cache_key(ws, we, None, None, "b")

    def test_none_and_empty_ingestion_id_differ(self):
        from src.api.routes.explain import _cache_key
        ws = datetime(2026, 3, 12, 13, 0, 0, tzinfo=timezone.utc)
        we = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)
        assert _cache_key(ws, we, None, None, None) != _cache_key(ws, we, None, None, "")
