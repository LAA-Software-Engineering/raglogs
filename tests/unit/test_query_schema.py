"""Contract tests for the versioned /v1/query JSON evidence schema (G7)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.api.app import app
from src.api.schemas.v1 import (
    SCHEMA_VERSION,
    AskResponse,
    ClustersResponse,
    CompareResponse,
    ExplainResponse,
    SimilarResponse,
    TimelineResponse,
    explain_from_cached,
    short_summary,
)
from src.core.normalization.patterns import infer_trigger_type

client = TestClient(app, raise_server_exceptions=False)
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "clients" / "jsonschema"
WINDOW_START = datetime(2026, 3, 12, 13, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)


def _ctx_db() -> MagicMock:
    mock_db = MagicMock()
    mock_db.__enter__ = MagicMock(return_value=mock_db)
    mock_db.__exit__ = MagicMock(return_value=False)
    mock_db.execute.return_value.scalar_one.return_value = 0
    return mock_db


def _explain_result(**overrides: object) -> MagicMock:
    result = MagicMock()
    result.window_start = WINDOW_START
    result.window_end = WINDOW_END
    result.summary_text = (
        "Incident summary\n\n"
        "Window: 2026-03-12 13:00 UTC → 2026-03-12 14:00 UTC\n"
        "Primary issue: Stripe signature verification failed\n"
        "Confidence: high"
    )
    result.confidence = "high"
    result.mode = "rules"
    result.total_logs = 404
    result.services_affected = ["api"]
    result.primary_cluster = None
    result.secondary_clusters = []
    result.trigger_candidates = []
    result.evidence_items = ["184 similar failures in billing-worker"]
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


def test_published_jsonschema_matches_pydantic() -> None:
    mapping = {
        "explain.v1.json": ExplainResponse,
        "timeline.v1.json": TimelineResponse,
        "compare.v1.json": CompareResponse,
        "ask.v1.json": AskResponse,
        "clusters.v1.json": ClustersResponse,
        "similar.v1.json": SimilarResponse,
    }
    for filename, model in mapping.items():
        path = SCHEMA_DIR / filename
        assert path.is_file(), f"missing {path}"
        published = json.loads(path.read_text(encoding="utf-8"))
        assert published == model.model_json_schema()


def test_short_summary_extracts_primary_issue() -> None:
    prose = "Incident summary\n\nWindow: 1h\nPrimary issue: payment timeouts\n"
    assert short_summary(prose) == "payment timeouts"


def test_infer_trigger_type() -> None:
    assert infer_trigger_type("Deploy completed for api") == "deploy"
    assert infer_trigger_type("circuit breaker tripped") == "circuit_breaker"
    assert infer_trigger_type("hello world") is None


def test_cached_old_payload_upgrades_to_v1() -> None:
    old = {
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "summary": "Incident summary\n\nPrimary issue: old cache hit",
        "confidence": "medium-high",
        "mode": "rules",
        "total_logs": 10,
        "services_affected": ["api"],
        "primary_cluster": None,
        "secondary_clusters": [],
        "trigger_candidates": [],
        "evidence": ["a log line"],
    }
    body = explain_from_cached(
        old,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        no_llm=False,
        scope="default",
    )
    data = body.model_dump(by_alias=True)
    ExplainResponse.model_validate(data)
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["window"]["from"] == WINDOW_START.isoformat()
    assert data["confidence"]["label"] == "medium-high"
    assert data["confidence"]["score"] == 0.72
    assert data["trigger"]["detected"] is False
    assert data["evidence"][0]["kind"] == "log"
    assert data["evidence"][0]["detail"] == "a log line"
    assert data["llm"]["used"] is False
    assert "provider" in data["llm"]
    assert "model" in data["llm"]
    assert "fell_back" in data["llm"]
    assert data["cached"] is True


def _openapi_properties(model: dict, components: dict) -> dict:
    props = dict(model.get("properties") or {})
    for item in model.get("allOf") or []:
        if "$ref" in item:
            name = item["$ref"].rsplit("/", 1)[-1]
            props.update(_openapi_properties(components[name], components))
        elif isinstance(item, dict):
            props.update(_openapi_properties(item, components))
    return props


def test_explain_request_documents_optional_overrides() -> None:
    schema = app.openapi()
    components = schema["components"]["schemas"]
    explain_req = None
    for name, model in components.items():
        if name.endswith("ExplainRequest") or name == "ExplainRequest":
            explain_req = model
            break
    assert explain_req is not None
    props = _openapi_properties(explain_req, components)
    for key in ("baseline_window", "max_clusters", "max_evidence_items", "llm"):
        assert key in props
    required = explain_req.get("required") or []
    for key in ("baseline_window", "max_clusters", "max_evidence_items", "llm"):
        assert key not in required


def test_explain_openapi_documents_response_model() -> None:
    schema = app.openapi()
    explain = schema["paths"]["/v1/query/explain"]["post"]
    ref = explain["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in ref
    assert "ExplainResponse" in ref["$ref"]
    components = schema["components"]["schemas"]
    assert "ExplainResponse" in components
    assert "LlmProvenance" in components
    llm = components["LlmProvenance"]["properties"]
    for key in ("used", "provider", "model", "fell_back"):
        assert key in llm


def test_explain_and_unversioned_share_v1_body() -> None:
    mock_db = _ctx_db()
    with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
         patch("src.core.explain.summarizer.explain_window", return_value=_explain_result()), \
         patch("src.api.routes.explain._load_from_cache", return_value=None), \
         patch("src.api.routes.explain._save_to_cache"):
        unversioned = client.post("/query/explain", json={"since": "1h", "no_llm": True})
        versioned = client.post("/v1/query/explain", json={"since": "1h", "no_llm": True})

    assert unversioned.status_code == 200
    assert versioned.status_code == 200
    assert unversioned.json() == versioned.json()
    ExplainResponse.model_validate(versioned.json())
    assert versioned.json()["schema_version"] == "1.0"


def test_similar_openapi_documents_response_model() -> None:
    schema = app.openapi()
    similar = schema["paths"]["/v1/query/similar"]["post"]
    ref = similar["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in ref
    assert "SimilarResponse" in ref["$ref"]
    components = schema["components"]["schemas"]
    assert "SimilarResponse" in components
    assert "SimilarMatchModel" in components
