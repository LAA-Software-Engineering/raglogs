"""Unit tests for G8 scope isolation (no database)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.api.app import app
from src.api.auth.middleware import AuthPrincipal
from src.api.auth.scope import (
    ERROR_SCOPE_MISMATCH,
    ERROR_SCOPE_REQUIRED,
    ScopeResolutionError,
    resolve_scope,
)
from src.config.settings import Settings
from src.core.clustering.baseline import get_baseline_counts
from src.core.retrieval.question_router import search_logs, search_logs_semantic
from src.db.models import IngestionJob, LogEntry
from src.db.scope_filter import (
    filter_ingestion_jobs_by_scope,
    filter_log_entries_by_scope,
)

client = TestClient(app, raise_server_exceptions=False)

WINDOW_START = datetime(2026, 8, 16, 5, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 16, 7, 0, tzinfo=timezone.utc)


def _principal(
    *,
    scope: str = "incident:A",
    allow_scope_override: bool = False,
    auth_method: str = "api_key",
    role: str = "query",
) -> AuthPrincipal:
    return AuthPrincipal(
        role=role,
        scope=scope,
        auth_method=auth_method,
        key_id=str(uuid4()),
        allow_scope_override=allow_scope_override,
    )


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": False})).lower()


def _params(stmt) -> list[object]:
    return list(stmt.compile(compile_kwargs={"literal_binds": False}).params.values())


def _auth_settings(**kwargs: object) -> Settings:
    values: dict[str, object] = {"auth_enabled": True, "auth_mode": "api_key"}
    values.update(kwargs)
    return Settings(_env_file=None, **values)


def _key(
    role: str,
    *,
    scope: str = "default",
    allow_scope_override: bool = False,
) -> MagicMock:
    record = MagicMock()
    record.id = uuid4()
    record.name = "test"
    record.key_prefix = "rlk_testhash"
    record.role = role
    record.scope = scope
    record.allow_scope_override = allow_scope_override
    record.revoked_at = None
    return record


def _ctx_db() -> MagicMock:
    mock_db = MagicMock()
    mock_db.__enter__ = MagicMock(return_value=mock_db)
    mock_db.__exit__ = MagicMock(return_value=False)
    mock_db.execute.return_value.scalar_one.return_value = 0
    mock_db.execute.return_value.all.return_value = []
    mock_db.execute.return_value.scalars.return_value.all.return_value = []
    mock_db.execute.return_value.scalar_one_or_none.return_value = None
    return mock_db


# ── Resolution ────────────────────────────────────────────────────────────────


class TestResolveScope:
    def test_auth_off_defaults_to_default(self) -> None:
        assert (
            resolve_scope(requested_scope=None, auth_enabled=False, principal=None)
            == "default"
        )

    def test_auth_off_uses_explicit_request_scope(self) -> None:
        assert (
            resolve_scope(
                requested_scope="incident:INC-9",
                auth_enabled=False,
                principal=None,
            )
            == "incident:INC-9"
        )

    def test_auth_off_strips_whitespace(self) -> None:
        assert (
            resolve_scope(
                requested_scope="  env:prod  ",
                auth_enabled=False,
                principal=None,
            )
            == "env:prod"
        )

    def test_auth_off_blank_request_falls_back_to_default(self) -> None:
        assert (
            resolve_scope(requested_scope="   ", auth_enabled=False, principal=None)
            == "default"
        )

    def test_pinned_uses_key_scope(self) -> None:
        assert (
            resolve_scope(
                requested_scope=None,
                auth_enabled=True,
                principal=_principal(scope="incident:A"),
            )
            == "incident:A"
        )

    def test_pinned_same_request_scope_ok(self) -> None:
        assert (
            resolve_scope(
                requested_scope="incident:A",
                auth_enabled=True,
                principal=_principal(scope="incident:A"),
            )
            == "incident:A"
        )

    def test_pinned_mismatch_raises(self) -> None:
        with pytest.raises(ScopeResolutionError) as exc:
            resolve_scope(
                requested_scope="incident:B",
                auth_enabled=True,
                principal=_principal(scope="incident:A"),
            )
        assert exc.value.status_code == 403
        assert exc.value.error_code == ERROR_SCOPE_MISMATCH

    def test_oidc_is_pinned_even_if_override_flag_were_set(self) -> None:
        principal = AuthPrincipal(
            role="query",
            scope="default",
            auth_method="oidc",
            allow_scope_override=True,
        )
        with pytest.raises(ScopeResolutionError) as exc:
            resolve_scope(
                requested_scope="incident:X",
                auth_enabled=True,
                principal=principal,
            )
        assert exc.value.error_code == ERROR_SCOPE_MISMATCH

    def test_override_uses_request_scope(self) -> None:
        assert (
            resolve_scope(
                requested_scope="incident:B",
                auth_enabled=True,
                principal=_principal(
                    scope="incident:A", allow_scope_override=True
                ),
            )
            == "incident:B"
        )

    def test_override_falls_back_to_key_scope(self) -> None:
        assert (
            resolve_scope(
                requested_scope=None,
                auth_enabled=True,
                principal=_principal(
                    scope="incident:A", allow_scope_override=True
                ),
            )
            == "incident:A"
        )

    def test_override_both_empty_is_scope_required(self) -> None:
        with pytest.raises(ScopeResolutionError) as exc:
            resolve_scope(
                requested_scope="  ",
                auth_enabled=True,
                principal=_principal(scope="", allow_scope_override=True),
            )
        assert exc.value.status_code == 400
        assert exc.value.error_code == ERROR_SCOPE_REQUIRED

    def test_auth_on_without_principal_is_scope_required(self) -> None:
        with pytest.raises(ScopeResolutionError) as exc:
            resolve_scope(
                requested_scope=None, auth_enabled=True, principal=None
            )
        assert exc.value.error_code == ERROR_SCOPE_REQUIRED

    def test_pinned_empty_key_scope_is_scope_required(self) -> None:
        with pytest.raises(ScopeResolutionError) as exc:
            resolve_scope(
                requested_scope=None,
                auth_enabled=True,
                principal=_principal(scope="  "),
            )
        assert exc.value.error_code == ERROR_SCOPE_REQUIRED


# ── SQL isolation ─────────────────────────────────────────────────────────────


class TestScopeFilterSQL:
    def test_log_entry_helper_binds_scope(self) -> None:
        stmt = filter_log_entries_by_scope(select(LogEntry), "incident:A")
        sql = _compiled(stmt)
        assert "log_entries.scope" in sql
        assert "incident:A" in _params(stmt)

    def test_ingestion_job_helper_binds_scope(self) -> None:
        stmt = filter_ingestion_jobs_by_scope(select(IngestionJob), "incident:B")
        sql = _compiled(stmt)
        assert "ingestion_jobs.scope" in sql
        assert "incident:B" in _params(stmt)

    def test_two_scopes_compile_to_different_binds(self) -> None:
        a = filter_log_entries_by_scope(select(LogEntry), "incident:A")
        b = filter_log_entries_by_scope(select(LogEntry), "incident:B")
        assert "incident:A" in _params(a)
        assert "incident:B" in _params(b)
        assert "incident:B" not in _params(a)
        assert "incident:A" not in _params(b)

    def test_baseline_sql_includes_scope(self) -> None:
        db = MagicMock()
        db.execute.return_value.all.return_value = []
        get_baseline_counts(
            db, WINDOW_START, WINDOW_END, scope="incident:A"
        )
        stmt = db.execute.call_args[0][0]
        sql = _compiled(stmt)
        assert "log_entries.scope" in sql
        assert "incident:A" in _params(stmt)

    def test_search_logs_sql_includes_scope(self) -> None:
        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = []
        search_logs(
            db, ["error"], WINDOW_START, WINDOW_END, None, None, scope="incident:A"
        )
        stmt = db.execute.call_args[0][0]
        sql = _compiled(stmt)
        assert "log_entries.scope" in sql
        assert "incident:A" in _params(stmt)

    def test_semantic_search_sql_includes_scope(self) -> None:
        from src.core.embeddings.store import STORED_EMBEDDING_DIMS

        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = []
        search_logs_semantic(
            db,
            [0.01] * STORED_EMBEDDING_DIMS,
            WINDOW_START,
            WINDOW_END,
            None,
            None,
            scope="incident:A",
        )
        stmt = db.execute.call_args[0][0]
        sql = _compiled(stmt)
        assert "log_entries.scope" in sql
        assert "incident:A" in _params(stmt)


# ── API TestClient (mocked DB) ────────────────────────────────────────────────


class TestScopeApi:
    def test_auth_off_explain_passes_default_scope(self) -> None:
        mock_result = MagicMock()
        mock_result.window_start = WINDOW_START
        mock_result.window_end = WINDOW_END
        mock_result.summary_text = "ok"
        mock_result.confidence = "low"
        mock_result.mode = "rules"
        mock_result.total_logs = 0
        mock_result.services_affected = []
        mock_result.primary_cluster = None
        mock_result.secondary_clusters = []
        mock_result.trigger_candidates = []
        mock_result.evidence_items = []
        mock_db = _ctx_db()

        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.explain_window",
                 return_value=mock_result,
             ) as mock_explain, \
             patch("src.api.routes.explain._load_from_cache", return_value=None), \
             patch("src.api.routes.explain._save_to_cache"):
            resp = client.post("/v1/query/explain", json={"since": "1h", "no_llm": True})

        assert resp.status_code == 200
        assert resp.json()["scope"] == "default"
        assert mock_explain.call_args.kwargs["scope"] == "default"

    def test_auth_off_explicit_request_scope(self) -> None:
        mock_result = MagicMock()
        mock_result.window_start = WINDOW_START
        mock_result.window_end = WINDOW_END
        mock_result.summary_text = "ok"
        mock_result.confidence = "low"
        mock_result.mode = "rules"
        mock_result.total_logs = 0
        mock_result.services_affected = []
        mock_result.primary_cluster = None
        mock_result.secondary_clusters = []
        mock_result.trigger_candidates = []
        mock_result.evidence_items = []
        mock_db = _ctx_db()

        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.explain_window",
                 return_value=mock_result,
             ) as mock_explain, \
             patch("src.api.routes.explain._load_from_cache", return_value=None), \
             patch("src.api.routes.explain._save_to_cache"):
            resp = client.post(
                "/v1/query/explain",
                json={"since": "1h", "no_llm": True, "scope": "incident:CLI"},
            )

        assert resp.status_code == 200
        assert resp.json()["scope"] == "incident:CLI"
        assert mock_explain.call_args.kwargs["scope"] == "incident:CLI"

    def test_pinned_key_mismatch_is_403(self) -> None:
        settings = _auth_settings()
        with patch("src.config.get_settings", return_value=settings), \
             patch(
                 "src.api.auth.keys.lookup_api_key",
                 return_value=_key("query", scope="incident:A"),
             ):
            resp = client.post(
                "/v1/query/explain",
                json={"since": "1h", "scope": "incident:B"},
                headers={"Authorization": "Bearer rlk_queryrolexx"},
            )
        assert resp.status_code == 403
        body = resp.json()
        assert body["error_code"] == ERROR_SCOPE_MISMATCH
        assert "detail" not in body

    def test_override_key_uses_request_scope(self) -> None:
        mock_result = MagicMock()
        mock_result.window_start = WINDOW_START
        mock_result.window_end = WINDOW_END
        mock_result.summary_text = "ok"
        mock_result.confidence = "low"
        mock_result.mode = "rules"
        mock_result.total_logs = 0
        mock_result.services_affected = []
        mock_result.primary_cluster = None
        mock_result.secondary_clusters = []
        mock_result.trigger_candidates = []
        mock_result.evidence_items = []
        mock_db = _ctx_db()
        settings = _auth_settings()
        key = _key("query", scope="incident:A", allow_scope_override=True)

        with patch("src.config.get_settings", return_value=settings), \
             patch("src.api.auth.keys.lookup_api_key", return_value=key), \
             patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.explain_window",
                 return_value=mock_result,
             ) as mock_explain, \
             patch("src.api.routes.explain._load_from_cache", return_value=None), \
             patch("src.api.routes.explain._save_to_cache"):
            resp = client.post(
                "/v1/query/explain",
                json={"since": "1h", "no_llm": True, "scope": "incident:B"},
                headers={"Authorization": "Bearer rlk_queryrolexx"},
            )

        assert resp.status_code == 200
        assert resp.json()["scope"] == "incident:B"
        assert mock_explain.call_args.kwargs["scope"] == "incident:B"

    def test_two_keys_cannot_list_each_others_ingestions(self) -> None:
        settings = _auth_settings()
        mock_db = _ctx_db()

        def _list(scope: str) -> None:
            with patch("src.config.get_settings", return_value=settings), \
                 patch(
                     "src.api.auth.keys.lookup_api_key",
                     return_value=_key("query", scope=scope),
                 ), \
                 patch("src.db.session.get_db", side_effect=lambda: mock_db):
                resp = client.get(
                    "/v1/ingestions",
                    headers={"Authorization": "Bearer rlk_queryrolexx"},
                )
            assert resp.status_code == 200
            stmt = mock_db.execute.call_args[0][0]
            sql = _compiled(stmt)
            assert "ingestion_jobs.scope" in sql
            assert scope in _params(stmt)

        _list("incident:A")
        _list("incident:B")

    def test_ingest_stamps_resolved_scope_on_worker_payload(self) -> None:
        settings = _auth_settings()
        key = _key("ingest", scope="incident:INC-9")
        mock_db = _ctx_db()
        added: list[object] = []

        def capture_add(obj: object) -> None:
            added.append(obj)
            obj.id = uuid4()  # type: ignore[attr-defined]

        mock_db.add.side_effect = capture_add

        with patch("src.config.get_settings", return_value=settings), \
             patch("src.api.auth.keys.lookup_api_key", return_value=key), \
             patch(
                 "src.adapters.file.adapter.discover_files",
                 return_value=["f.log"],
             ), \
             patch("src.db.session.get_db", side_effect=lambda: mock_db):
            resp = client.post(
                "/v1/ingestions",
                json={"paths": ["/logs"]},
                headers={"Authorization": "Bearer rlk_ingestrole1"},
            )

        assert resp.status_code == 202
        jobs = [obj for obj in added if hasattr(obj, "payload_json")]
        assert jobs[0].payload_json["scope"] == "incident:INC-9"
