"""G10 LLM resilience: timeout/retry wrapper, token budget, circuit breaker.

Call order (outer → inner), shared by ``generate_summary`` and ask HTTP:

  CappedLLMProvider          # G9 process-wide in-flight cap
    ResilientLLMProvider     # this module: breaker → token budget → retries
      OpenAI / Ollama        # single HTTP attempt with LLM_TIMEOUT / LLM_MAX_TOKENS

Noop is not wrapped, so ``LLM_PROVIDER=disabled`` never trips the breaker.
While the breaker is open the provider is not called (no failure increment).
A cooldown expiry moves the breaker to half-open; one probe is allowed.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any, Literal

import httpx
import structlog
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.config.settings import Settings

log = structlog.get_logger()

BreakerState = Literal["closed", "open", "half_open"]

_LIST_KEYS: tuple[str, ...] = (
    "evidence",
    "clusters",
    "secondary_clusters",
    "trigger_candidates",
)


class LLMCircuitOpen(Exception):
    """Raised when the process-local LLM breaker is open (or a probe is in flight)."""


class LLMBudgetExceeded(Exception):
    """Raised when the evidence payload still exceeds the input-token budget after trim."""


class LLMCircuitBreaker:
    """Process-local consecutive-failure breaker (same lifetime as the G9 semaphore)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: int = 0
        self._opened_at: float | None = None
        self._probe_in_flight: bool = False

    def snapshot(
        self,
        *,
        threshold: int,
        cooldown_seconds: float,
    ) -> dict[str, Any]:
        with self._lock:
            state, remaining = self._state_unlocked(cooldown_seconds)
            return {
                "state": state,
                "consecutive_failures": self._failures,
                "cooldown_remaining_seconds": remaining,
            }

    def allow_request(self, *, threshold: int, cooldown_seconds: float) -> bool:
        """Return True if the provider may be called.

        ``threshold`` is accepted for a stable call signature with ``snapshot`` /
        ``record_failure``; open/closed is driven by ``_opened_at``.
        """
        del threshold
        with self._lock:
            state, _remaining = self._state_unlocked(cooldown_seconds)
            if state == "closed":
                return True
            if state == "open":
                return False
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    def record_failure(self, *, threshold: int, cooldown_seconds: float) -> None:
        del cooldown_seconds
        with self._lock:
            self._probe_in_flight = False
            self._failures += 1
            if threshold > 0 and self._failures >= threshold:
                self._opened_at = time.monotonic()

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    def _state_unlocked(self, cooldown_seconds: float) -> tuple[BreakerState, float]:
        if self._opened_at is None:
            return "closed", 0.0
        elapsed = time.monotonic() - self._opened_at
        remaining = round(max(0.0, cooldown_seconds - elapsed), 1)
        if remaining > 0:
            return "open", remaining
        return "half_open", 0.0


_breaker_lock = threading.Lock()
_breaker: LLMCircuitBreaker | None = None


def get_llm_breaker() -> LLMCircuitBreaker:
    global _breaker
    with _breaker_lock:
        if _breaker is None:
            _breaker = LLMCircuitBreaker()
        return _breaker


def reset_llm_breaker() -> None:
    """Drop consecutive-failure state so tests can start from closed."""
    get_llm_breaker().reset()


def breaker_health() -> dict[str, Any]:
    """Snapshot for ``GET /health`` using current settings."""
    from src.config import get_settings

    settings = get_settings()
    return get_llm_breaker().snapshot(
        threshold=settings.llm_breaker_threshold,
        cooldown_seconds=settings.llm_breaker_cooldown_seconds,
    )


def default_llm_wait() -> wait_exponential_jitter:
    """Jittered exponential backoff between LLM HTTP attempts."""
    return wait_exponential_jitter(initial=0.5, max=8)


def estimate_tokens(payload: Any) -> int:
    """Cheap token estimate: UTF-8 chars / 4 (min 1)."""
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, default=str, indent=2)
    return max(1, (len(text) + 3) // 4)


def input_token_budget(settings: Settings) -> int:
    if settings.llm_max_input_tokens > 0:
        return settings.llm_max_input_tokens
    return max(512, int(settings.llm_max_tokens) * 8)


def trim_evidence_packet(packet: dict[str, Any], max_items: int) -> dict[str, Any]:
    """Copy ``packet`` and cap list-valued evidence fields at ``max_items``."""
    trimmed = dict(packet)
    cap = max(0, max_items)
    for key in _LIST_KEYS:
        value = trimmed.get(key)
        if isinstance(value, list) and len(value) > cap:
            trimmed[key] = value[:cap]
    return trimmed


def prepare_llm_packet(packet: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Trim evidence to ``max_evidence_items``, then shrink until under budget.

    Raises ``LLMBudgetExceeded`` if the payload is still too large (e.g. a
    single huge primary cluster). Callers fall back to templates.
    """
    trimmed = trim_evidence_packet(packet, settings.max_evidence_items)
    budget = input_token_budget(settings)
    if estimate_tokens(trimmed) <= budget:
        return trimmed

    while estimate_tokens(trimmed) > budget:
        shrunk = False
        for key in _LIST_KEYS:
            value = trimmed.get(key)
            if isinstance(value, list) and len(value) > 0:
                trimmed[key] = value[:-1]
                shrunk = True
                break
        if not shrunk:
            raise LLMBudgetExceeded(
                f"evidence payload ~{estimate_tokens(trimmed)} tokens "
                f"exceeds budget {budget}"
            )
    return trimmed


def _is_retryable_llm_error(exc: BaseException) -> bool:
    if isinstance(exc, (LLMCircuitOpen, LLMBudgetExceeded)):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    if isinstance(exc, httpx.HTTPError):
        return True
    return True


def invoke_llm(attempt: Callable[[], str]) -> str:
    """Run ``attempt`` with breaker + bounded jittered retries.

    One exhausted retry sequence counts as a single consecutive failure.
    ``attempt`` should perform a single HTTP call (timeout lives on httpx).
    """
    from src.config import get_settings

    settings = get_settings()
    breaker = get_llm_breaker()
    threshold = settings.llm_breaker_threshold
    cooldown = settings.llm_breaker_cooldown_seconds

    if not breaker.allow_request(threshold=threshold, cooldown_seconds=cooldown):
        snap = breaker.snapshot(threshold=threshold, cooldown_seconds=cooldown)
        log.warning("llm_circuit_open", **snap)
        raise LLMCircuitOpen("LLM circuit breaker is open")

    result = ""
    try:
        for retry_state in Retrying(
            stop=stop_after_attempt(max(1, int(settings.llm_max_retries) + 1)),
            wait=default_llm_wait(),
            retry=retry_if_exception(_is_retryable_llm_error),
            reraise=True,
        ):
            with retry_state:
                result = attempt()
    except (LLMCircuitOpen, LLMBudgetExceeded):
        raise
    except Exception:
        breaker.record_failure(threshold=threshold, cooldown_seconds=cooldown)
        log.warning("llm_call_failed", exc_info=True)
        raise

    breaker.record_success()
    return result
