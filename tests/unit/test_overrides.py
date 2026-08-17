"""Per-request query config overrides (G14). No database required."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.api.auth.keys import ApiKeyRecord
from src.api.auth.middleware import AuthPrincipal
from src.api.overrides import (
    ERROR_INVALID_OVERRIDE,
    MAX_CLUSTERS_MAX,
    MAX_CLUSTERS_MIN,
    MAX_EVIDENCE_ITEMS_MAX,
    MAX_EVIDENCE_ITEMS_MIN,
    OverrideInput,
    OverrideValidationError,
    build_key_config_json,
    merge_key_config,
    resolve_query_overrides,
)
from src.config.settings import Settings
from src.core.llm.provider import NoopLLMProvider, build_llm_provider, unwrap_llm_provider

client = TestClient(app, raise_server_exceptions=False)

WINDOW_START = datetime(2026, 3, 12, 13, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)


def _settings(**kwargs: object) -> Settings:
    values: dict[str, object] = {
        "default_baseline_window": "24h",
        "max_clusters_for_explain": 10,
        "max_evidence_items": 8,
        "llm_provider": "disabled",
        "auth_enabled": False,
    }
    values.update(kwargs)
    return Settings(_env_file=None, **values)


def _principal(config: dict | None = None) -> AuthPrincipal:
    return AuthPrincipal(
        role="query",
        scope="default",
        auth_method="api_key",
        key_id=str(uuid.uuid4()),
        config_json=config,
    )


def _resolve(
    request: OverrideInput,
    *,
    settings: Settings | None = None,
    principal: AuthPrincipal | None = None,
    auth_enabled: bool = False,
) -> object:
    return resolve_query_overrides(
        request,
        principal,
        settings or _settings(),
        auth_enabled=auth_enabled,
    )


def _explain_result() -> MagicMock:
    result = MagicMock()
    result.window_start = WINDOW_START
    result.window_end = WINDOW_END
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


def _ctx_db() -> MagicMock:
    mock_db = MagicMock()
    mock_db.__enter__ = MagicMock(return_value=mock_db)
    mock_db.__exit__ = MagicMock(return_value=False)
    mock_db.execute.return_value.scalar_one.return_value = 0
    return mock_db


def _record(**kwargs: object) -> ApiKeyRecord:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "name": "test",
        "key_prefix": "rlk_testhash",
        "key_hash": "hash",
        "role": "query",
        "scope": "default",
        "revoked_at": None,
        "config_json": None,
    }
    values.update(kwargs)
    return ApiKeyRecord(**values)  # type: ignore[arg-type]


class TestResolvePrecedence:
    def test_omitted_fields_use_server_defaults(self) -> None:
        settings = _settings(
            default_baseline_window="12h",
            max_clusters_for_explain=7,
            max_evidence_items=3,
            llm_provider="disabled",
        )
        resolved = _resolve(OverrideInput(), settings=settings, auth_enabled=False)
        assert resolved.baseline_window == "12h"
        assert resolved.max_clusters == 7
        assert resolved.max_evidence_items == 3
        assert resolved.llm_provider == "disabled"
        assert resolved.llm_enabled is False

    def test_request_wins_over_key_defaults(self) -> None:
        settings = _settings(max_clusters_for_explain=10, max_evidence_items=8)
        principal = _principal(
            {
                "max_clusters": 4,
                "max_evidence_items": 2,
                "baseline_window": "6h",
                "llm": {"provider": "ollama", "enabled": True},
            }
        )
        resolved = _resolve(
            OverrideInput(
                max_clusters=9,
                max_evidence_items=5,
                baseline_window="48h",
                llm_provider="openai",
                llm_enabled=False,
            ),
            settings=settings,
            principal=principal,
            auth_enabled=True,
        )
        assert resolved.max_clusters == 9
        assert resolved.max_evidence_items == 5
        assert resolved.baseline_window == "48h"
        assert resolved.llm_provider == "openai"
        assert resolved.llm_enabled is False

    def test_key_defaults_win_over_server_when_request_omits(self) -> None:
        settings = _settings(
            max_clusters_for_explain=10,
            max_evidence_items=8,
            default_baseline_window="24h",
            llm_provider="disabled",
        )
        principal = _principal(
            {
                "max_clusters": 4,
                "max_evidence_items": 2,
                "baseline_window": "6h",
                "llm": {"provider": "ollama"},
            }
        )
        resolved = _resolve(
            OverrideInput(),
            settings=settings,
            principal=principal,
            auth_enabled=True,
        )
        assert resolved.max_clusters == 4
        assert resolved.max_evidence_items == 2
        assert resolved.baseline_window == "6h"
        assert resolved.llm_provider == "ollama"
        assert resolved.llm_enabled is True

    def test_auth_off_skips_key_layer(self) -> None:
        settings = _settings(max_clusters_for_explain=10, llm_provider="disabled")
        principal = _principal({"max_clusters": 99, "llm": {"provider": "ollama"}})
        resolved = _resolve(
            OverrideInput(max_clusters=3),
            settings=settings,
            principal=principal,
            auth_enabled=False,
        )
        assert resolved.max_clusters == 3
        assert resolved.llm_provider == "disabled"
        assert resolved.llm_enabled is False

        omitted = _resolve(
            OverrideInput(),
            settings=settings,
            principal=principal,
            auth_enabled=False,
        )
        assert omitted.max_clusters == 10
        assert omitted.llm_provider == "disabled"

    def test_llm_enabled_false_acts_like_no_llm(self) -> None:
        settings = _settings(llm_provider="openai", openai_api_key="sk-test")
        resolved = _resolve(
            OverrideInput(llm_enabled=False),
            settings=settings,
        )
        assert resolved.llm_provider == "openai"
        assert resolved.llm_enabled is False
        assert resolved.no_llm is True

    def test_no_llm_true_disables_when_llm_object_omitted(self) -> None:
        settings = _settings(llm_provider="ollama")
        resolved = _resolve(OverrideInput(no_llm=True), settings=settings)
        assert resolved.llm_enabled is False

    def test_request_provider_openai_enables_when_server_disabled(self) -> None:
        settings = _settings(llm_provider="disabled")
        resolved = _resolve(
            OverrideInput(llm_provider="openai"),
            settings=settings,
        )
        assert resolved.llm_provider == "openai"
        assert resolved.llm_enabled is True


class TestResolveValidation:
    def test_max_clusters_below_min_raises(self) -> None:
        with pytest.raises(OverrideValidationError) as exc:
            _resolve(OverrideInput(max_clusters=0))
        assert exc.value.error_code == ERROR_INVALID_OVERRIDE
        assert exc.value.field == "max_clusters"
        assert exc.value.min_value == MAX_CLUSTERS_MIN
        assert exc.value.max_value == MAX_CLUSTERS_MAX

    def test_max_clusters_above_max_raises(self) -> None:
        with pytest.raises(OverrideValidationError) as exc:
            _resolve(OverrideInput(max_clusters=101))
        assert exc.value.field == "max_clusters"
        body = exc.value.as_dict()
        assert body["min"] == 1
        assert body["max"] == 100

    def test_max_evidence_items_out_of_bounds_raises(self) -> None:
        with pytest.raises(OverrideValidationError) as exc:
            _resolve(OverrideInput(max_evidence_items=0))
        assert exc.value.field == "max_evidence_items"
        assert exc.value.min_value == MAX_EVIDENCE_ITEMS_MIN
        assert exc.value.max_value == MAX_EVIDENCE_ITEMS_MAX

        with pytest.raises(OverrideValidationError) as exc:
            _resolve(OverrideInput(max_evidence_items=51))
        assert exc.value.field == "max_evidence_items"

    def test_invalid_baseline_window_raises(self) -> None:
        with pytest.raises(OverrideValidationError) as exc:
            _resolve(OverrideInput(baseline_window="not-a-duration"))
        assert exc.value.field == "baseline_window"
        assert "duration" in exc.value.message.lower() or "parse" in exc.value.message.lower()

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(OverrideValidationError) as exc:
            _resolve(OverrideInput(llm_provider="gemini"))
        assert exc.value.field == "llm.provider"
        assert "gemini" in exc.value.message

    def test_claude_provider_is_accepted(self) -> None:
        resolved = _resolve(OverrideInput(llm_provider="claude"))
        assert resolved.llm_provider == "claude"
        assert resolved.llm_enabled is True

    def test_openai_without_key_still_noop(self) -> None:
        settings = _settings(llm_provider="disabled", openai_api_key="")
        resolved = _resolve(
            OverrideInput(llm_provider="openai"),
            settings=settings,
        )
        overlaid = settings.model_copy(update={"llm_provider": resolved.llm_provider})
        provider = unwrap_llm_provider(build_llm_provider(overlaid))
        assert isinstance(provider, NoopLLMProvider)

    def test_claude_without_key_still_noop(self) -> None:
        settings = _settings(llm_provider="disabled", anthropic_api_key="")
        resolved = _resolve(
            OverrideInput(llm_provider="claude"),
            settings=settings,
        )
        overlaid = settings.model_copy(update={"llm_provider": resolved.llm_provider})
        provider = unwrap_llm_provider(build_llm_provider(overlaid))
        assert isinstance(provider, NoopLLMProvider)


class TestKeyConfigJson:
    def test_build_and_merge(self) -> None:
        built = build_key_config_json(max_clusters=5, llm_provider="ollama")
        assert built == {"max_clusters": 5, "llm": {"provider": "ollama"}}
        merged = merge_key_config(built, max_evidence_items=3, llm_enabled=False)
        assert merged["max_clusters"] == 5
        assert merged["max_evidence_items"] == 3
        assert merged["llm"]["provider"] == "ollama"
        assert merged["llm"]["enabled"] is False
        assert merge_key_config(merged, clear=True) is None

    def test_build_rejects_out_of_bounds(self) -> None:
        with pytest.raises(OverrideValidationError):
            build_key_config_json(max_clusters=0)
        with pytest.raises(OverrideValidationError):
            build_key_config_json(llm_provider="gemini")
        built = build_key_config_json(llm_provider="claude")
        assert built == {"llm": {"provider": "claude"}}


class TestExplainApiOverrides:
    def test_omitted_max_clusters_uses_server_setting(self) -> None:
        settings = _settings(max_clusters_for_explain=3, max_evidence_items=2, default_baseline_window="6h")
        mock_db = _ctx_db()
        with patch("src.config.get_settings", return_value=settings), \
             patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.explain_window",
                 return_value=_explain_result(),
             ) as mock_explain, \
             patch("src.api.routes.explain._load_from_cache", return_value=None), \
             patch("src.api.routes.explain._save_to_cache"):
            resp = client.post("/v1/query/explain", json={"since": "1h", "no_llm": True})

        assert resp.status_code == 200
        kwargs = mock_explain.call_args.kwargs
        assert kwargs["max_clusters"] == 3
        assert kwargs["max_evidence_items"] == 2
        assert kwargs["baseline_window_str"] == "6h"
        assert kwargs["no_llm"] is True

    def test_request_max_clusters_passed_through(self) -> None:
        settings = _settings(max_clusters_for_explain=10)
        mock_db = _ctx_db()
        with patch("src.config.get_settings", return_value=settings), \
             patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.explain_window",
                 return_value=_explain_result(),
             ) as mock_explain, \
             patch("src.api.routes.explain._load_from_cache", return_value=None), \
             patch("src.api.routes.explain._save_to_cache"):
            resp = client.post(
                "/v1/query/explain",
                json={"since": "1h", "no_llm": True, "max_clusters": 6},
            )

        assert resp.status_code == 200
        assert mock_explain.call_args.kwargs["max_clusters"] == 6

    def test_out_of_bounds_returns_typed_400(self) -> None:
        resp = client.post(
            "/v1/query/explain",
            json={"since": "1h", "max_clusters": 0},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error_code"] == ERROR_INVALID_OVERRIDE
        assert body["field"] == "max_clusters"
        assert body["min"] == MAX_CLUSTERS_MIN
        assert body["max"] == MAX_CLUSTERS_MAX

    def test_invalid_provider_returns_typed_400(self) -> None:
        resp = client.post(
            "/v1/query/explain",
            json={"since": "1h", "llm": {"provider": "gemini"}},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error_code"] == ERROR_INVALID_OVERRIDE
        assert body["field"] == "llm.provider"

    def test_claude_provider_is_accepted_on_explain(self) -> None:
        settings = _settings(llm_provider="disabled")
        mock_db = _ctx_db()
        with patch("src.config.get_settings", return_value=settings), \
             patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.explain_window",
                 return_value=_explain_result(),
             ) as mock_explain, \
             patch("src.api.routes.explain._load_from_cache", return_value=None), \
             patch("src.api.routes.explain._save_to_cache"):
            resp = client.post(
                "/v1/query/explain",
                json={"since": "1h", "no_llm": True, "llm": {"provider": "claude"}},
            )

        assert resp.status_code == 200
        assert mock_explain.call_args.kwargs["llm_provider"] == "claude"

    def test_invalid_baseline_returns_typed_400(self) -> None:
        resp = client.post(
            "/v1/query/explain",
            json={"since": "1h", "baseline_window": "yesterday"},
        )
        assert resp.status_code == 400
        assert resp.json()["error_code"] == ERROR_INVALID_OVERRIDE
        assert resp.json()["field"] == "baseline_window"

    def test_auth_on_key_defaults_apply_when_request_omits(self) -> None:
        settings = _settings(
            auth_enabled=True,
            max_clusters_for_explain=10,
            max_evidence_items=8,
        )
        record = _record(
            config_json={"max_clusters": 4, "max_evidence_items": 2, "baseline_window": "6h"}
        )
        mock_db = _ctx_db()
        with patch("src.config.get_settings", return_value=settings), \
             patch("src.api.auth.keys.lookup_api_key", return_value=record), \
             patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.explain_window",
                 return_value=_explain_result(),
             ) as mock_explain, \
             patch("src.api.routes.explain._load_from_cache", return_value=None), \
             patch("src.api.routes.explain._save_to_cache"):
            resp = client.post(
                "/v1/query/explain",
                json={"since": "1h", "no_llm": True},
                headers={"Authorization": "Bearer rlk_queryrolexx"},
            )

        assert resp.status_code == 200
        kwargs = mock_explain.call_args.kwargs
        assert kwargs["max_clusters"] == 4
        assert kwargs["max_evidence_items"] == 2
        assert kwargs["baseline_window_str"] == "6h"

    def test_auth_on_request_wins_over_key_defaults(self) -> None:
        settings = _settings(auth_enabled=True, max_clusters_for_explain=10)
        record = _record(config_json={"max_clusters": 4})
        mock_db = _ctx_db()
        with patch("src.config.get_settings", return_value=settings), \
             patch("src.api.auth.keys.lookup_api_key", return_value=record), \
             patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.explain_window",
                 return_value=_explain_result(),
             ) as mock_explain, \
             patch("src.api.routes.explain._load_from_cache", return_value=None), \
             patch("src.api.routes.explain._save_to_cache"):
            resp = client.post(
                "/v1/query/explain",
                json={"since": "1h", "no_llm": True, "max_clusters": 9},
                headers={"Authorization": "Bearer rlk_queryrolexx"},
            )

        assert resp.status_code == 200
        assert mock_explain.call_args.kwargs["max_clusters"] == 9

    def test_cache_key_includes_resolved_overrides(self) -> None:
        from src.api.overrides import ResolvedOverrides
        from src.api.routes.explain import _cache_key

        a = ResolvedOverrides(
            baseline_window="24h",
            max_clusters=10,
            max_evidence_items=8,
            llm_provider="disabled",
            llm_enabled=False,
        )
        b = ResolvedOverrides(
            baseline_window="24h",
            max_clusters=5,
            max_evidence_items=8,
            llm_provider="disabled",
            llm_enabled=False,
        )
        k1 = _cache_key(WINDOW_START, WINDOW_END, None, None, None, overrides=a)
        k2 = _cache_key(WINDOW_START, WINDOW_END, None, None, None, overrides=b)
        k1b = _cache_key(WINDOW_START, WINDOW_END, None, None, None, overrides=a)
        assert k1 == k1b
        assert k1 != k2
