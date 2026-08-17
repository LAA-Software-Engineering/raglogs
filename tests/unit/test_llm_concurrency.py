"""Unit tests for the process-wide LLM concurrency cap (no database)."""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

from src.config.settings import Settings
from src.core.llm.provider import (
    CappedLLMProvider,
    NoopLLMProvider,
    build_llm_provider,
    reset_llm_concurrency_limiter,
    unwrap_llm_provider,
)


def test_build_wraps_noop_and_generate_summary_returns_empty() -> None:
    settings = Settings(_env_file=None, llm_provider="disabled")
    llm = build_llm_provider(settings)
    assert isinstance(llm, CappedLLMProvider)
    assert isinstance(unwrap_llm_provider(llm), NoopLLMProvider)
    assert llm.generate_summary({"x": 1}) == ""


def test_semaphore_serializes_inner_provider_calls() -> None:
    reset_llm_concurrency_limiter()
    settings = Settings(_env_file=None, llm_max_concurrency=1)
    inner_current = 0
    max_seen = 0
    lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    class FakeProvider:
        def generate_summary(self, evidence_packet: dict) -> str:
            nonlocal inner_current, max_seen
            with lock:
                inner_current += 1
                max_seen = max(max_seen, inner_current)
            started.set()
            assert release.wait(timeout=5)
            with lock:
                inner_current -= 1
            return "ok"

    capped = CappedLLMProvider(FakeProvider())
    with patch("src.config.get_settings", return_value=settings):
        t1 = threading.Thread(target=lambda: capped.generate_summary({}))
        t2 = threading.Thread(target=lambda: capped.generate_summary({}))
        t1.start()
        assert started.wait(timeout=2)
        t2.start()
        time.sleep(0.15)
        assert max_seen == 1
        release.set()
        t1.join(timeout=2)
        t2.join(timeout=2)

    assert max_seen == 1
    reset_llm_concurrency_limiter()


def test_noop_does_not_block_when_semaphore_is_held() -> None:
    reset_llm_concurrency_limiter()
    settings = Settings(_env_file=None, llm_max_concurrency=1)
    started = threading.Event()
    block = threading.Event()

    class BlockingProvider:
        def generate_summary(self, evidence_packet: dict) -> str:
            started.set()
            assert block.wait(timeout=5)
            return "real"

    real = CappedLLMProvider(BlockingProvider())
    noop = CappedLLMProvider(NoopLLMProvider())
    with patch("src.config.get_settings", return_value=settings):
        holder = threading.Thread(target=lambda: real.generate_summary({}))
        holder.start()
        assert started.wait(timeout=2)
        t0 = time.monotonic()
        assert noop.generate_summary({}) == ""
        assert time.monotonic() - t0 < 0.5
        block.set()
        holder.join(timeout=2)
    reset_llm_concurrency_limiter()
