"""OpenAPI contract tests — fail CI if a canonical /v1 path is removed.

Loads the schema from ``app.openapi()`` (no live server). JSON response
shapes are not asserted here; that is G7.
"""

from __future__ import annotations

from src.api.app import app
from src.api.deprecation import (
    is_deprecated_alias,
    is_versioned_api_path,
    successor_path,
)

REQUIRED_GET: tuple[str, ...] = (
    "/health",
    "/v1/ingestions",
    "/v1/config",
)

REQUIRED_POST: tuple[str, ...] = (
    "/v1/query/explain",
    "/v1/query/ask",
    "/v1/query/timeline",
    "/v1/query/compare",
    "/v1/query/clusters",
    "/v1/ingestions",
    "/v1/ingestions/lines",
    "/v1/ingestions/{ingestion_job_id}:pause",
    "/v1/ingestions/{ingestion_job_id}:resume",
    "/v1/ingestions/{ingestion_job_id}:stop",
)


def test_canonical_v1_paths_exist() -> None:
    paths = app.openapi()["paths"]
    for path in (*REQUIRED_GET, *REQUIRED_POST):
        assert path in paths, f"missing OpenAPI path {path}"


def test_required_methods() -> None:
    paths = app.openapi()["paths"]
    for path in REQUIRED_GET:
        assert "get" in paths[path], f"{path} must allow GET"
    for path in REQUIRED_POST:
        assert "post" in paths[path], f"{path} must allow POST"


def test_v1_operations_are_not_deprecated() -> None:
    paths = app.openapi()["paths"]
    explain = paths["/v1/query/explain"]["post"]
    assert not explain.get("deprecated")
    ingest = paths["/v1/ingestions"]["post"]
    assert not ingest.get("deprecated")


def test_unversioned_aliases_are_deprecated_in_schema() -> None:
    paths = app.openapi()["paths"]
    assert "/query/explain" in paths
    assert paths["/query/explain"]["post"].get("deprecated") is True
    assert paths["/ingestions"]["post"].get("deprecated") is True
    assert paths["/config"]["get"].get("deprecated") is True


def test_ingest_request_includes_callback_url() -> None:
    schema = app.openapi()["components"]["schemas"]["IngestRequest"]
    assert "callback_url" in schema["properties"]


def test_ingest_post_documents_idempotency_key_header() -> None:
    paths = app.openapi()["paths"]
    for path in ("/v1/ingestions", "/ingestions"):
        params = paths[path]["post"].get("parameters") or []
        names = {p.get("name") for p in params}
        assert "Idempotency-Key" in names, f"{path} POST must document Idempotency-Key"
        header = next(p for p in params if p.get("name") == "Idempotency-Key")
        assert header.get("in") == "header"
        assert header.get("required") is not True


def test_health_is_unversioned() -> None:
    paths = app.openapi()["paths"]
    assert "/health" in paths
    assert "/v1/health" not in paths
    assert "get" in paths["/health"]


def test_deprecation_helpers() -> None:
    assert is_deprecated_alias("/query/explain") is True
    assert is_deprecated_alias("/ingestions") is True
    assert is_deprecated_alias("/config") is True
    assert is_deprecated_alias("/v1/query/explain") is False
    assert is_deprecated_alias("/v1/ingestions") is False
    assert is_deprecated_alias("/health") is False
    assert is_deprecated_alias("/") is False
    assert is_deprecated_alias("/static/js/app.js") is False
    assert is_versioned_api_path("/v1/query/explain") is True
    assert is_versioned_api_path("/v2/ingestions") is True
    assert is_versioned_api_path("/query/explain") is False
    assert successor_path("/query/explain") == "/v1/query/explain"
    assert successor_path("/ingestions/latest") == "/v1/ingestions/latest"
