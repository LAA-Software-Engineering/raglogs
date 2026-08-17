"""Unit tests for G10 LLM resilience (no database)."""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from tenacity import wait_exponential_jitter, wait_none

from src.config.settings import Settings
from src.core.clustering.clusterer import ClusterData
from src.core.explain.confidence import compute_confidence
from src.core.explain.evidence import EvidencePacket
from src.core.explain.summarizer import explain_window
from src.core.explain.templates import render_text_summary
from src.core.llm.provider import (
    CappedLLMProvider,
    NoopLLMProvider,
    OllamaLLMProvider,
    OpenAILLMProvider,
    ResilientLLMProvider,
    build_llm_provider,
    unwrap_llm_provider,
)
from src.core.llm.resilience import (
    LLMBudgetExceeded,
    LLMCircuitOpen,
    default_llm_wait,
    estimate_tokens,
    invoke_llm,
    prepare_llm_packet,
    reset_llm_breaker,
    trim_evidence_packet,
)
from src.core.retrieval.question_router import _call_llm_ask, _rules_answer, answer_question
from src.db.models import LogEntry

WINDOW_START = datetime(2026, 3, 12, 13, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 3, 12, 14, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_breaker() -> Iterator[None]:
    reset_llm_breaker()
    yield
    reset_llm_breaker()


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)


def _cluster(message: str, count: int = 50) -> ClusterData:
    return ClusterData(
        fingerprint="abcd1234",
        representative_message=message,
        count=count,
        services={"checkout": count},
        levels={"error": count},
        first_seen=WINDOW_START,
        last_seen=WINDOW_END,
        baseline_count=0,
        change_ratio=51.0,
        importance_score=8.0,
    )


def _packet(message: str = "payment gateway 502") -> EvidencePacket:
    return EvidencePacket(
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        total_logs=404,
        primary_cluster=_cluster(message),
        secondary_clusters=[],
        trigger_candidates=[],
        evidence_items=[f"184 similar failures: {message}"],
        services_affected=["checkout"],
    )


class _BoomProvider:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls = 0
        self.exc = exc or RuntimeError("provider down")

    def generate_summary(self, evidence_packet: dict) -> str:
        self.calls += 1
        raise self.exc


class _FlakyProvider:
    def __init__(self, fail_times: int) -> None:
        self.calls = 0
        self.fail_times = fail_times

    def generate_summary(self, evidence_packet: dict) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("transient")
        return "llm summary"


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _CapturingClient:
    last_timeout: float | None = None
    last_json: dict[str, Any] | None = None

    def __init__(self, *args: object, timeout: float | None = None, **kwargs: object) -> None:
        type(self).last_timeout = timeout

    def __enter__(self) -> _CapturingClient:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def post(self, *args: object, **kwargs: object) -> _FakeResponse:
        payload = kwargs.get("json")
        if isinstance(payload, dict):
            type(self).last_json = payload
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})


class _TimeoutClient(_CapturingClient):
    def post(self, *args: object, **kwargs: object) -> _FakeResponse:
        raise httpx.TimeoutException("deadline exceeded")


def test_disabled_build_skips_resilient_wrapper() -> None:
    llm = build_llm_provider(_settings(llm_provider="disabled"))
    assert isinstance(llm, CappedLLMProvider)
    assert isinstance(unwrap_llm_provider(llm), NoopLLMProvider)
    assert not isinstance(llm.inner, ResilientLLMProvider)
    assert llm.generate_summary({"x": 1}) == ""


def test_openai_build_wraps_resilient_inside_cap() -> None:
    llm = build_llm_provider(_settings(llm_provider="openai", openai_api_key="sk-test"))
    assert isinstance(llm, CappedLLMProvider)
    assert isinstance(llm.inner, ResilientLLMProvider)
    assert isinstance(unwrap_llm_provider(llm), OpenAILLMProvider)


def test_default_wait_is_exponential_jitter() -> None:
    assert isinstance(default_llm_wait(), wait_exponential_jitter)


def test_retries_then_succeeds_without_opening_breaker() -> None:
    inner = _FlakyProvider(fail_times=2)
    settings = _settings(llm_max_retries=2, llm_breaker_threshold=5)
    with patch("src.config.get_settings", return_value=settings), \
         patch("src.core.llm.resilience.default_llm_wait", return_value=wait_none()):
        text = invoke_llm(lambda: inner.generate_summary({}))
    assert text == "llm summary"
    assert inner.calls == 3
    from src.core.llm.resilience import breaker_health

    with patch("src.config.get_settings", return_value=settings):
        health = breaker_health()
    assert health["state"] == "closed"
    assert health["consecutive_failures"] == 0


def test_exhausted_retries_count_as_one_failure() -> None:
    inner = _BoomProvider()
    settings = _settings(llm_max_retries=2, llm_breaker_threshold=5)
    with patch("src.config.get_settings", return_value=settings), \
         patch("src.core.llm.resilience.default_llm_wait", return_value=wait_none()):
        with pytest.raises(RuntimeError, match="provider down"):
            invoke_llm(lambda: inner.generate_summary({}))
    assert inner.calls == 3
    from src.core.llm.resilience import breaker_health

    with patch("src.config.get_settings", return_value=settings):
        health = breaker_health()
    assert health["state"] == "closed"
    assert health["consecutive_failures"] == 1


def test_breaker_opens_then_skips_provider() -> None:
    inner = _BoomProvider()
    settings = _settings(llm_max_retries=0, llm_breaker_threshold=2, llm_breaker_cooldown_seconds=60)
    llm = ResilientLLMProvider(inner)
    with patch("src.config.get_settings", return_value=settings), \
         patch("src.core.llm.resilience.default_llm_wait", return_value=wait_none()):
        with pytest.raises(RuntimeError):
            llm.generate_summary({"evidence": ["a"]})
        with pytest.raises(RuntimeError):
            llm.generate_summary({"evidence": ["a"]})
        with pytest.raises(LLMCircuitOpen):
            llm.generate_summary({"evidence": ["a"]})
    assert inner.calls == 2


def test_breaker_success_resets() -> None:
    calls = {"n": 0}

    def _attempt() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("once")
        return "ok"

    settings = _settings(llm_max_retries=0, llm_breaker_threshold=5)
    with patch("src.config.get_settings", return_value=settings), \
         patch("src.core.llm.resilience.default_llm_wait", return_value=wait_none()):
        with pytest.raises(RuntimeError):
            invoke_llm(_attempt)
        assert invoke_llm(_attempt) == "ok"
    from src.core.llm.resilience import breaker_health

    with patch("src.config.get_settings", return_value=settings):
        health = breaker_health()
    assert health["state"] == "closed"
    assert health["consecutive_failures"] == 0


def test_half_open_probe_after_cooldown() -> None:
    inner = _BoomProvider()
    settings = _settings(
        llm_max_retries=0,
        llm_breaker_threshold=1,
        llm_breaker_cooldown_seconds=0.0,
    )
    llm = ResilientLLMProvider(inner)
    with patch("src.config.get_settings", return_value=settings), \
         patch("src.core.llm.resilience.default_llm_wait", return_value=wait_none()):
        with pytest.raises(RuntimeError):
            llm.generate_summary({"evidence": ["a"]})
        # cooldown 0 → half-open; next call is a probe (provider called again)
        with pytest.raises(RuntimeError):
            llm.generate_summary({"evidence": ["a"]})
    assert inner.calls == 2


def test_trim_respects_max_evidence_items() -> None:
    settings = _settings(max_evidence_items=2, llm_max_input_tokens=100_000)
    packet = {
        "evidence": ["a", "b", "c", "d"],
        "clusters": ["w", "x", "y"],
        "primary_cluster": {"message": "payment gateway 502"},
    }
    trimmed = trim_evidence_packet(packet, settings.max_evidence_items)
    assert trimmed["evidence"] == ["a", "b"]
    assert trimmed["clusters"] == ["w", "x"]
    assert trimmed["primary_cluster"]["message"] == "payment gateway 502"


def test_prepare_falls_back_when_payload_still_over_budget() -> None:
    settings = _settings(max_evidence_items=8, llm_max_input_tokens=8)
    packet = {"primary_cluster": {"message": "x" * 5000}}
    with pytest.raises(LLMBudgetExceeded):
        prepare_llm_packet(packet, settings)
    assert estimate_tokens(packet) > 8


def test_openai_uses_configured_timeout_and_max_tokens() -> None:
    _CapturingClient.last_timeout = None
    _CapturingClient.last_json = None
    settings = _settings(llm_timeout=12.0, llm_max_tokens=80)
    provider = OpenAILLMProvider(api_key="sk-test")
    with patch("src.config.get_settings", return_value=settings), \
         patch("httpx.Client", _CapturingClient):
        assert provider.generate_summary({"a": 1}) == "ok"
    assert _CapturingClient.last_timeout == 12.0
    assert _CapturingClient.last_json is not None
    assert _CapturingClient.last_json["max_tokens"] == 80


def test_ollama_uses_num_predict_from_settings() -> None:
    _CapturingClient.last_json = None

    class _OllamaClient(_CapturingClient):
        def post(self, *args: object, **kwargs: object) -> _FakeResponse:
            payload = kwargs.get("json")
            if isinstance(payload, dict):
                type(self).last_json = payload
            return _FakeResponse({"response": "local ok"})

    settings = _settings(llm_timeout=9.0, llm_max_tokens=120)
    provider = OllamaLLMProvider(model="llama3")
    with patch("src.config.get_settings", return_value=settings), \
         patch("httpx.Client", _OllamaClient):
        assert provider.generate_summary({"a": 1}) == "local ok"
    assert _OllamaClient.last_json is not None
    assert _OllamaClient.last_json["options"]["num_predict"] == 120


def test_explain_timeout_falls_back_preserving_evidence() -> None:
    packet = _packet("payment gateway 502")
    settings = _settings(llm_provider="openai", openai_api_key="sk-test")
    boom = _BoomProvider(exc=httpx.TimeoutException("deadline"))

    with patch("src.core.explain.summarizer.run_clustering", return_value=(None, [packet.primary_cluster])), \
         patch("src.core.explain.summarizer.assemble_evidence", return_value=packet), \
         patch("src.core.explain.summarizer.get_settings", return_value=settings), \
         patch("src.core.explain.summarizer.build_llm_provider", return_value=boom):
        result = explain_window(
            db=MagicMock(),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )

    expected = render_text_summary(packet, compute_confidence(packet))
    assert result.mode == "rules"
    assert result.summary_text == expected
    assert "payment gateway 502" in result.summary_text
    assert "184 similar failures: payment gateway 502" in result.summary_text
    assert "invented root cause" not in result.summary_text.lower()


def test_explain_open_breaker_falls_back_without_calling_inner() -> None:
    packet = _packet("connection refused by redis")
    inner = _BoomProvider()
    settings = _settings(
        llm_provider="openai",
        openai_api_key="sk-test",
        llm_max_retries=0,
        llm_breaker_threshold=1,
        llm_breaker_cooldown_seconds=60,
    )
    llm = CappedLLMProvider(ResilientLLMProvider(inner))

    with patch("src.config.get_settings", return_value=settings), \
         patch("src.core.llm.resilience.default_llm_wait", return_value=wait_none()), \
         patch("src.core.explain.summarizer.run_clustering", return_value=(None, [packet.primary_cluster])), \
         patch("src.core.explain.summarizer.assemble_evidence", return_value=packet), \
         patch("src.core.explain.summarizer.get_settings", return_value=settings), \
         patch("src.core.explain.summarizer.build_llm_provider", return_value=llm):
        first = explain_window(db=MagicMock(), window_start=WINDOW_START, window_end=WINDOW_END)
        second = explain_window(db=MagicMock(), window_start=WINDOW_START, window_end=WINDOW_END)

    assert inner.calls == 1
    assert first.mode == "rules"
    assert second.mode == "rules"
    assert "connection refused by redis" in second.summary_text
    assert second.summary_text == render_text_summary(packet, compute_confidence(packet))


def test_ask_timeout_falls_back_to_rules_answer() -> None:
    hit = LogEntry(
        id=uuid.uuid4(),
        timestamp=WINDOW_START,
        service="checkout",
        level="error",
        raw_message="payment gateway 502 from checkout",
        normalized_message="payment gateway 502 from checkout",
        fingerprint="fp-pay-502",
    )
    settings = _settings(
        llm_provider="openai",
        openai_api_key="sk-test",
        llm_max_retries=0,
        embeddings_provider="disabled",
    )
    llm = OpenAILLMProvider(api_key="sk-test")
    from src.core.embeddings.provider import DisabledEmbeddingsProvider

    with patch("src.config.get_settings", return_value=settings), \
         patch("src.core.retrieval.question_router.get_embeddings_provider", return_value=DisabledEmbeddingsProvider()), \
         patch("src.core.retrieval.question_router.search_logs", return_value=[hit]), \
         patch("src.core.llm.provider.build_llm_provider", return_value=llm), \
         patch("src.core.llm.resilience.default_llm_wait", return_value=wait_none()), \
         patch("httpx.Client", _TimeoutClient):
        result = answer_question(
            MagicMock(),
            "why are payments failing?",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )

    assert result.mode == "rules"
    assert "payment gateway 502" in result.answer_text
    expected = _rules_answer(result.question, result.clusters_used, result.total_matches)
    assert result.answer_text == expected


def test_call_llm_ask_timeout_raises_after_retries() -> None:
    settings = _settings(llm_max_retries=1, llm_timeout=1.0)
    llm = OpenAILLMProvider(api_key="sk-test")
    with patch("src.config.get_settings", return_value=settings), \
         patch("src.core.llm.resilience.default_llm_wait", return_value=wait_none()), \
         patch("httpx.Client", _TimeoutClient):
        with pytest.raises(httpx.TimeoutException):
            _call_llm_ask(llm, "why?", {"clusters": [{"message": "x"}]})
