"""Unit tests for API token-bucket rate limiting (no database)."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.api.auth.middleware import AuthPrincipal
from src.api.ratelimit import (
    allow_request,
    bucket_identity,
    rate_limit_kind,
    reset_rate_limiter,
)
from src.config.settings import Settings

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_buckets() -> None:
    reset_rate_limiter()
    yield
    reset_rate_limiter()


def _tiny_settings(**kwargs: object) -> Settings:
    values: dict[str, object] = {
        "ratelimit_enabled": True,
        "ratelimit_ingest_rps": 1.0,
        "ratelimit_query_rps": 1.0,
        "ratelimit_burst": 1.0,
        "ratelimit_retry_after_seconds": 7,
        "auth_enabled": False,
    }
    values.update(kwargs)
    return Settings(_env_file=None, **values)


def _key(role: str, key_id: uuid.UUID | None = None) -> MagicMock:
    record = MagicMock()
    record.id = key_id or uuid.uuid4()
    record.name = "test"
    record.key_prefix = "rlk_testhash"
    record.role = role
    record.scope = "default"
    record.revoked_at = None
    record.allow_scope_override = False
    return record


class TestRateLimitKind:
    def test_ingest_writes_only(self) -> None:
        assert rate_limit_kind("POST", "/v1/ingestions") == "ingest"
        assert rate_limit_kind("POST", "/ingestions") == "ingest"
        assert rate_limit_kind("POST", "/v1/ingestions/lines") == "ingest"
        assert rate_limit_kind("POST", "/v1/ingestions/abc:pause") == "ingest"
        assert rate_limit_kind("GET", "/v1/ingestions") is None
        assert rate_limit_kind("GET", "/v1/ingestions/latest") is None

    def test_query_routes_versioned_and_alias(self) -> None:
        assert rate_limit_kind("POST", "/v1/query/explain") == "query"
        assert rate_limit_kind("POST", "/query/explain") == "query"
        assert rate_limit_kind("POST", "/v1/query/ask") == "query"
        assert rate_limit_kind("GET", "/v1/query/clusters") == "query"

    def test_exempt_paths(self) -> None:
        assert rate_limit_kind("GET", "/health") is None
        assert rate_limit_kind("GET", "/docs") is None
        assert rate_limit_kind("GET", "/static/js/app.js") is None
        assert rate_limit_kind("GET", "/v1/config") is None
        assert rate_limit_kind("GET", "/") is None


class TestTokenBucket:
    def test_allow_then_deny_and_per_key_isolation(self) -> None:
        assert allow_request("ingest", "key-a", rate=1.0, burst=1.0) is True
        assert allow_request("ingest", "key-a", rate=1.0, burst=1.0) is False
        assert allow_request("ingest", "key-b", rate=1.0, burst=1.0) is True

    def test_zero_rate_is_unlimited(self) -> None:
        for _ in range(20):
            assert allow_request("query", "anon", rate=0.0, burst=1.0) is True

    def test_ingest_and_query_buckets_are_separate(self) -> None:
        assert allow_request("ingest", "same", rate=1.0, burst=1.0) is True
        assert allow_request("ingest", "same", rate=1.0, burst=1.0) is False
        assert allow_request("query", "same", rate=1.0, burst=1.0) is True


class TestBucketIdentity:
    def test_anonymous_without_principal(self) -> None:
        request = MagicMock()
        request.state.auth_principal = None
        assert bucket_identity(request) == "anonymous"

    def test_uses_key_id_when_present(self) -> None:
        request = MagicMock()
        request.state.auth_principal = AuthPrincipal(
            role="ingest",
            scope="default",
            auth_method="api_key",
            key_id="abc-123",
        )
        assert bucket_identity(request) == "abc-123"


class TestRateLimitHttp:
    def test_allow_then_429_with_retry_after_on_query(self) -> None:
        settings = _tiny_settings()
        with patch("src.config.get_settings", return_value=settings):
            first = client.post("/v1/query/explain", json={})
            second = client.post("/v1/query/explain", json={})

        assert first.status_code != 429
        assert second.status_code == 429
        body = second.json()
        assert body["error_code"] == "RATE_LIMITED"
        assert "message" in body
        assert second.headers.get("retry-after") == "7"

    def test_unversioned_query_alias_is_limited(self) -> None:
        settings = _tiny_settings()
        with patch("src.config.get_settings", return_value=settings):
            client.post("/query/explain", json={})
            second = client.post("/query/explain", json={})
        assert second.status_code == 429
        assert second.json()["error_code"] == "RATE_LIMITED"

    def test_ingest_write_429_distinct_from_queue_full(self) -> None:
        settings = _tiny_settings()
        with patch("src.config.get_settings", return_value=settings):
            first = client.post("/v1/ingestions", json={})
            second = client.post("/v1/ingestions", json={})

        assert first.status_code == 422
        assert second.status_code == 429
        assert second.json()["error_code"] == "RATE_LIMITED"
        assert second.json()["error_code"] != "INGEST_QUEUE_FULL"

    def test_health_not_limited(self) -> None:
        settings = _tiny_settings()
        with patch("src.config.get_settings", return_value=settings), \
             patch("src.db.session.check_connection", return_value=False):
            for _ in range(5):
                resp = client.get("/health")
                assert resp.status_code == 200

    def test_config_not_limited(self) -> None:
        settings = _tiny_settings()
        with patch("src.config.get_settings", return_value=settings):
            for _ in range(5):
                resp = client.get("/v1/config")
                assert resp.status_code == 200

    def test_zero_rps_is_unlimited(self) -> None:
        settings = _tiny_settings(ratelimit_query_rps=0.0, ratelimit_burst=1.0)
        with patch("src.config.get_settings", return_value=settings):
            statuses = [
                client.post("/v1/query/explain", json={}).status_code
                for _ in range(8)
            ]
        assert 429 not in statuses

    def test_disabled_flag_skips_limiting(self) -> None:
        settings = _tiny_settings(ratelimit_enabled=False)
        with patch("src.config.get_settings", return_value=settings):
            statuses = [
                client.post("/v1/query/explain", json={}).status_code
                for _ in range(5)
            ]
        assert 429 not in statuses

    def test_default_settings_do_not_429_existing_client_traffic(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.auth_enabled is False
        with patch("src.config.get_settings", return_value=settings):
            statuses = [
                client.post("/v1/query/explain", json={}).status_code
                for _ in range(15)
            ]
        assert 429 not in statuses

    def test_synthetic_load_exhausts_tiny_bucket(self) -> None:
        settings = _tiny_settings(ratelimit_query_rps=1.0, ratelimit_burst=2.0)
        with patch("src.config.get_settings", return_value=settings):
            statuses = [
                client.post("/v1/query/explain", json={"since": "1h"}).status_code
                for _ in range(10)
            ]
        assert statuses[0] != 429
        assert statuses[1] != 429
        assert statuses.count(429) >= 8

    def test_per_key_isolation_when_auth_enabled(self) -> None:
        key_a_id = uuid.uuid4()
        key_b_id = uuid.uuid4()
        key_a = _key("admin", key_a_id)
        key_b = _key("admin", key_b_id)

        def lookup(token: str) -> MagicMock | None:
            if token == "rlk_key_aaaaaa":
                return key_a
            if token == "rlk_key_bbbbbb":
                return key_b
            return None

        settings = _tiny_settings(
            auth_enabled=True,
            ratelimit_ingest_rps=1.0,
            ratelimit_burst=1.0,
        )
        with patch("src.config.get_settings", return_value=settings), \
             patch("src.api.auth.keys.lookup_api_key", side_effect=lookup):
            a1 = client.post(
                "/v1/ingestions",
                json={},
                headers={"Authorization": "Bearer rlk_key_aaaaaa"},
            )
            a2 = client.post(
                "/v1/ingestions",
                json={},
                headers={"Authorization": "Bearer rlk_key_aaaaaa"},
            )
            b1 = client.post(
                "/v1/ingestions",
                json={},
                headers={"Authorization": "Bearer rlk_key_bbbbbb"},
            )

        assert a1.status_code == 422
        assert a2.status_code == 429
        assert a2.json()["error_code"] == "RATE_LIMITED"
        assert b1.status_code == 422

    def test_queue_full_still_ingest_queue_full_not_rate_limited(self) -> None:
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.execute.return_value.scalar_one.return_value = 100
        settings = Settings(_env_file=None)
        with (
            patch("src.config.get_settings", return_value=settings),
            patch("src.adapters.file.adapter.discover_files", return_value=["f.log"]),
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
        ):
            resp = client.post("/v1/ingestions", json={"paths": ["/logs"]})

        assert resp.status_code == 429
        assert resp.json()["error_code"] == "INGEST_QUEUE_FULL"
        assert resp.headers.get("retry-after") == "5"
