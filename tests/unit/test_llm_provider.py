"""Unit tests for the Claude LLM provider and factory (no database, no live API)."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from tenacity import wait_none

from src.config.settings import Settings
from src.core.clustering.clusterer import ClusterData
from src.core.explain.confidence import compute_confidence
from src.core.explain.evidence import EvidencePacket
from src.core.explain.summarizer import explain_window
from src.core.explain.templates import render_text_summary
from src.core.llm.provider import (
    SYSTEM_PROMPT,
    CappedLLMProvider,
    ClaudeLLMProvider,
    NoopLLMProvider,
    OpenAILLMProvider,
    ResilientLLMProvider,
    build_llm_provider,
    unwrap_llm_provider,
)
from src.core.llm.resilience import reset_llm_breaker
from src.core.retrieval.question_router import ASK_SYSTEM_PROMPT, _call_llm_ask

WINDOW_START = datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 3, 12, 14, 0, tzinfo=timezone.utc)


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)


def _packet(message: str = "payment gateway 502") -> EvidencePacket:
    cluster = ClusterData(
        fingerprint="abcd1234",
        representative_message=message,
        count=50,
        services={"checkout": 50},
        levels={"error": 50},
        first_seen=WINDOW_START,
        last_seen=WINDOW_END,
        baseline_count=0,
        change_ratio=51.0,
        importance_score=8.0,
    )
    return EvidencePacket(
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        total_logs=404,
        primary_cluster=cluster,
        secondary_clusters=[],
        trigger_candidates=[],
        evidence_items=[f"184 similar failures: {message}"],
        services_affected=["checkout"],
    )


class _FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        status_code: int = 200,
        error: Exception | None = None,
    ) -> None:
        self._payload = payload or {}
        self.status_code = status_code
        self._error = error
        self.request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _CapturingClient:
    last_url: str | None = None
    last_headers: dict[str, str] | None = None
    last_json: dict[str, Any] | None = None
    last_timeout: float | None = None
    response: _FakeResponse | None = None

    def __init__(self, *args: object, timeout: float | None = None, **kwargs: object) -> None:
        type(self).last_timeout = timeout

    def __enter__(self) -> _CapturingClient:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        type(self).last_url = url
        headers = kwargs.get("headers")
        payload = kwargs.get("json")
        type(self).last_headers = headers if isinstance(headers, dict) else None
        type(self).last_json = payload if isinstance(payload, dict) else None
        if type(self).response is not None:
            return type(self).response
        return _FakeResponse({"content": [{"type": "text", "text": "  claude summary  "}]})


@pytest.fixture(autouse=True)
def _reset_breaker() -> Iterator[None]:
    reset_llm_breaker()
    _CapturingClient.response = None
    _CapturingClient.last_url = None
    _CapturingClient.last_headers = None
    _CapturingClient.last_json = None
    yield
    reset_llm_breaker()
    _CapturingClient.response = None


def test_factory_returns_claude_when_configured() -> None:
    llm = build_llm_provider(
        _settings(
            llm_provider="claude",
            anthropic_api_key="sk-ant-test",
            llm_model="claude-haiku-4-5",
        )
    )
    assert isinstance(llm, CappedLLMProvider)
    assert isinstance(llm.inner, ResilientLLMProvider)
    inner = unwrap_llm_provider(llm)
    assert isinstance(inner, ClaudeLLMProvider)
    assert inner.model == "claude-haiku-4-5"
    assert inner.api_key == "sk-ant-test"
    assert inner.base_url == "https://api.anthropic.com"


def test_factory_returns_noop_when_claude_key_missing() -> None:
    llm = build_llm_provider(_settings(llm_provider="claude", anthropic_api_key=""))
    assert isinstance(llm, CappedLLMProvider)
    assert not isinstance(llm.inner, ResilientLLMProvider)
    assert isinstance(unwrap_llm_provider(llm), NoopLLMProvider)
    assert llm.generate_summary({"x": 1}) == ""


def test_openai_default_model_unchanged_when_provider_is_openai() -> None:
    settings = _settings(llm_provider="openai", openai_api_key="sk-test")
    assert settings.llm_model == "gpt-4.1-mini"
    inner = unwrap_llm_provider(build_llm_provider(settings))
    assert isinstance(inner, OpenAILLMProvider)
    assert inner.model == "gpt-4.1-mini"
    assert ClaudeLLMProvider(api_key="sk-ant-test").model == "claude-haiku-4-5"


def test_generate_summary_builds_messages_request_and_parses_text() -> None:
    settings = _settings(llm_timeout=12.0, llm_max_tokens=80)
    provider = ClaudeLLMProvider(
        api_key="sk-ant-test",
        model="claude-haiku-4-5",
        base_url="https://api.anthropic.com",
    )
    with patch("src.config.get_settings", return_value=settings), \
         patch("httpx.Client", _CapturingClient):
        text = provider.generate_summary({"primary": "payment 502"})

    assert text == "claude summary"
    assert _CapturingClient.last_url == "https://api.anthropic.com/v1/messages"
    assert _CapturingClient.last_timeout == 12.0
    headers = _CapturingClient.last_headers
    assert headers is not None
    assert headers["x-api-key"] == "sk-ant-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["content-type"] == "application/json"
    assert "Authorization" not in headers
    body = _CapturingClient.last_json
    assert body is not None
    assert body["model"] == "claude-haiku-4-5"
    assert body["max_tokens"] == 80
    assert body["system"] == SYSTEM_PROMPT
    assert body["messages"] == [
        {
            "role": "user",
            "content": "Analyze this incident evidence and produce a summary:\n\n"
            '{\n  "primary": "payment 502"\n}',
        }
    ]


def test_claude_http_error_does_not_crash_explain_pipeline() -> None:
    packet = _packet("payment gateway 502")
    settings = _settings(
        llm_provider="claude",
        anthropic_api_key="sk-ant-test",
        llm_model="claude-haiku-4-5",
        llm_max_retries=0,
    )
    _CapturingClient.response = _FakeResponse(
        error=httpx.HTTPStatusError(
            "500",
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            response=httpx.Response(500),
        )
    )
    llm = build_llm_provider(settings)
    with patch("src.config.get_settings", return_value=settings), \
         patch("src.core.explain.summarizer.get_settings", return_value=settings), \
         patch("src.core.explain.summarizer.run_clustering", return_value=(None, [packet.primary_cluster])), \
         patch("src.core.explain.summarizer.assemble_evidence", return_value=packet), \
         patch("src.core.explain.summarizer.build_llm_provider", return_value=llm), \
         patch("src.core.llm.resilience.default_llm_wait", return_value=wait_none()), \
         patch("httpx.Client", _CapturingClient):
        result = explain_window(
            db=MagicMock(),
            window_start=packet.window_start,
            window_end=packet.window_end,
        )

    expected = render_text_summary(packet, compute_confidence(packet))
    assert result.mode == "rules"
    assert result.summary_text == expected
    assert "payment gateway 502" in result.summary_text


def test_call_llm_ask_claude_posts_messages_api() -> None:
    settings = _settings(llm_timeout=9.0, llm_max_tokens=120, llm_max_retries=0)
    llm = ClaudeLLMProvider(api_key="sk-ant-ask", model="claude-haiku-4-5")
    with patch("src.config.get_settings", return_value=settings), \
         patch("src.core.llm.resilience.default_llm_wait", return_value=wait_none()), \
         patch("httpx.Client", _CapturingClient):
        text = _call_llm_ask(llm, "why did checkout fail?", {"clusters": [{"message": "502"}]})

    assert text == "claude summary"
    assert _CapturingClient.last_url == "https://api.anthropic.com/v1/messages"
    headers = _CapturingClient.last_headers
    assert headers is not None
    assert headers["x-api-key"] == "sk-ant-ask"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["content-type"] == "application/json"
    body = _CapturingClient.last_json
    assert body is not None
    assert body["system"] == ASK_SYSTEM_PROMPT
    assert body["model"] == "claude-haiku-4-5"
    assert body["max_tokens"] == 120
    assert body["messages"][0]["role"] == "user"
    assert "why did checkout fail?" in body["messages"][0]["content"]
    assert "502" in body["messages"][0]["content"]


def test_health_treats_claude_like_openai_for_missing_key() -> None:
    from src.api.routes.health import _llm_provider_health

    missing = _settings(llm_provider="claude", anthropic_api_key="")
    with patch("src.config.get_settings", return_value=missing):
        health = _llm_provider_health()
    assert health.provider == "claude"
    assert health.status == "unavailable: ANTHROPIC_API_KEY is not set"

    ok = _settings(llm_provider="claude", anthropic_api_key="sk-ant-test")
    with patch("src.config.get_settings", return_value=ok):
        health = _llm_provider_health()
    assert health.provider == "claude"
    assert health.status == "ok"
