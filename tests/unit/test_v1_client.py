"""Tests for the thin typed /v1 httpx client (no database)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.clients.v1 import RaglogsAPIError, RaglogsClient


def _explain_result() -> MagicMock:
    result = MagicMock()
    result.window_start = datetime(2026, 3, 12, 13, 0, 0, tzinfo=timezone.utc)
    result.window_end = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)
    result.summary_text = "ok"
    result.confidence = "low"
    result.mode = "rules"
    result.total_logs = 0
    result.services_affected = []
    result.primary_cluster = None
    result.secondary_clusters = []
    result.trigger_candidates = []
    result.evidence_items = []
    return result


def test_explain_targets_v1_and_returns_body() -> None:
    mock_db = MagicMock()
    mock_db.__enter__ = MagicMock(return_value=mock_db)
    mock_db.__exit__ = MagicMock(return_value=False)

    http = TestClient(app, raise_server_exceptions=False)
    with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
         patch("src.core.explain.summarizer.explain_window", return_value=_explain_result()), \
         patch("src.api.routes.explain._load_from_cache", return_value=None), \
         patch("src.api.routes.explain._save_to_cache"):
        client = RaglogsClient(base_url="http://testserver", client=http)
        payload = client.explain(since="1h", no_llm=True)

    assert payload["summary"] == "ok"
    assert payload["confidence"]["label"] == "low"
    assert payload["schema_version"] == "1.0"
    assert payload["llm"]["used"] is False


def test_health_uses_unversioned_path() -> None:
    http = TestClient(app, raise_server_exceptions=False)
    with patch("src.db.session.check_connection", return_value=False):
        client = RaglogsClient(base_url="http://testserver", client=http)
        payload = client.health()
    assert payload["status"] == "degraded"


def test_api_error_on_missing_window() -> None:
    http = TestClient(app, raise_server_exceptions=False)
    client = RaglogsClient(base_url="http://testserver", client=http)
    with pytest.raises(RaglogsAPIError) as exc:
        client.explain()
    assert exc.value.status_code == 400
