import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol, runtime_checkable

import httpx

from src.core.llm.resilience import invoke_llm, prepare_llm_packet


@runtime_checkable
class LLMProvider(Protocol):
    def generate_summary(self, evidence_packet: dict) -> str: ...


class NoopLLMProvider:
    """Fallback: returns empty string, triggers template-based summary."""
    def generate_summary(self, evidence_packet: dict) -> str:
        return ""


SYSTEM_PROMPT = """You are analyzing production logs to explain an incident.
Use ONLY the supplied evidence. Do not invent causes or events not present in the evidence.
Return a short incident summary with the following sections exactly:

Incident summary

Window: ...
Services affected: ...
Primary issue: ...
Secondary effects: ...
Likely trigger: ...

Evidence:
- ...

Confidence: low | medium | medium-high | high

If evidence is insufficient, say so clearly. Keep the entire output under 300 words. No markdown formatting."""


def _llm_timeout() -> float:
    from src.config import get_settings

    return float(get_settings().llm_timeout)


def _llm_max_tokens() -> int:
    from src.config import get_settings

    return int(get_settings().llm_max_tokens)


class OpenAILLMProvider:
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4.1-mini",
        base_url: str = "https://api.openai.com/v1",
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    def complete(self, system_prompt: str, user_message: str) -> str:
        """Single OpenAI chat-completions attempt (timeout/max_tokens from settings)."""
        with httpx.Client(timeout=_llm_timeout()) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": _llm_max_tokens(),
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()

    def generate_summary(self, evidence_packet: dict) -> str:
        payload = json.dumps(evidence_packet, default=str, indent=2)
        user_message = f"Analyze this incident evidence and produce a summary:\n\n{payload}"
        return self.complete(SYSTEM_PROMPT, user_message)


class OllamaLLMProvider:
    def __init__(
        self,
        model: str = "llama3",
        base_url: str = "http://localhost:11434",
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def complete(self, system_prompt: str, user_message: str) -> str:
        """Single Ollama /api/generate attempt (timeout/num_predict from settings)."""
        prompt = f"{system_prompt}\n\n{user_message}"
        with httpx.Client(timeout=_llm_timeout()) as client:
            response = client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0,
                        "num_predict": _llm_max_tokens(),
                    },
                },
            )
            response.raise_for_status()
            data = response.json()
            return data.get("response", "").strip()

    def generate_summary(self, evidence_packet: dict) -> str:
        payload = json.dumps(evidence_packet, default=str, indent=2)
        user_message = f"Incident evidence:\n{payload}\n\nSummary:"
        return self.complete(SYSTEM_PROMPT, user_message)


class ClaudeLLMProvider:
    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        base_url: str = "https://api.anthropic.com",
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    def complete(self, system_prompt: str, user_message: str) -> str:
        """Single Anthropic Messages API attempt (timeout/max_tokens from settings)."""
        with httpx.Client(timeout=_llm_timeout()) as client:
            response = client.post(
                f"{self.base_url}/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": _llm_max_tokens(),
                    "system": system_prompt,
                    "messages": [
                        {"role": "user", "content": user_message},
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["content"][0]["text"].strip()

    def generate_summary(self, evidence_packet: dict) -> str:
        payload = json.dumps(evidence_packet, default=str, indent=2)
        user_message = f"Analyze this incident evidence and produce a summary:\n\n{payload}"
        return self.complete(SYSTEM_PROMPT, user_message)


class ResilientLLMProvider:
    """G10 breaker + token budget + retries around an inner provider.

    Lives *inside* ``CappedLLMProvider`` so in-flight slots cover the whole
    retry sequence, but the breaker check still skips HTTP when open.
    """

    def __init__(self, inner: LLMProvider, settings: Any | None = None) -> None:
        self.inner = inner
        self._settings = settings

    def generate_summary(self, evidence_packet: dict) -> str:
        from src.config import get_settings
        from src.core.llm.resilience import estimate_tokens
        from src.observability.metrics import record_llm_estimated_tokens

        settings = self._settings if self._settings is not None else get_settings()
        prepared = prepare_llm_packet(evidence_packet, settings)
        record_llm_estimated_tokens(estimate_tokens(prepared))
        return invoke_llm(lambda: self.inner.generate_summary(prepared))

    def complete(self, system_prompt: str, user_message: str) -> str:
        inner = self.inner
        complete = getattr(inner, "complete", None)
        if complete is None:
            return ""
        return invoke_llm(lambda: complete(system_prompt, user_message))


_llm_sem_lock = threading.Lock()
_llm_semaphore: threading.Semaphore | None = None
_llm_semaphore_n: int | None = None


def reset_llm_concurrency_limiter() -> None:
    """Drop the process semaphore so tests can change LLM_MAX_CONCURRENCY."""
    global _llm_semaphore, _llm_semaphore_n
    with _llm_sem_lock:
        _llm_semaphore = None
        _llm_semaphore_n = None


def _semaphore_for(n: int) -> threading.Semaphore:
    global _llm_semaphore, _llm_semaphore_n
    with _llm_sem_lock:
        if _llm_semaphore is None or _llm_semaphore_n != n:
            _llm_semaphore = threading.Semaphore(n)
            _llm_semaphore_n = n
        return _llm_semaphore


@contextmanager
def llm_concurrency_slot(*, skip: bool = False) -> Iterator[None]:
    """Acquire the global LLM in-flight cap, or skip (noop / unlimited)."""
    if skip:
        yield
        return
    from src.config import get_settings

    n = int(get_settings().llm_max_concurrency)
    if n <= 0:
        yield
        return
    sem = _semaphore_for(n)
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


class CappedLLMProvider:
    """Wrap an inner provider with the process-wide concurrency semaphore.

    Noop inner providers skip the wait so deterministic mode never blocks,
    but still go through this entrypoint (CLI and API share the semaphore).

    Stack (outer → inner): CappedLLMProvider → ResilientLLMProvider → OpenAI/Ollama/Claude.
    Noop skips ResilientLLMProvider entirely.
    """

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner

    def generate_summary(self, evidence_packet: dict) -> str:
        skip = isinstance(unwrap_llm_provider(self), NoopLLMProvider)
        with llm_concurrency_slot(skip=skip):
            return self.inner.generate_summary(evidence_packet)


def unwrap_llm_provider(provider: LLMProvider) -> LLMProvider:
    """Return the inner provider if ``provider`` is concurrency-capped or resilient."""
    inner: LLMProvider = provider
    while isinstance(inner, (CappedLLMProvider, ResilientLLMProvider)):
        inner = inner.inner
    return inner


def _build_inner_llm_provider(settings: Any) -> LLMProvider:
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            return NoopLLMProvider()
        return OpenAILLMProvider(
            api_key=settings.openai_api_key,
            model=settings.llm_model,
            base_url=settings.openai_base_url,
        )
    if settings.llm_provider == "claude":
        if not settings.anthropic_api_key:
            return NoopLLMProvider()
        return ClaudeLLMProvider(
            api_key=settings.anthropic_api_key,
            model=settings.llm_model,
            base_url=settings.anthropic_base_url,
        )
    if settings.llm_provider == "ollama":
        return OllamaLLMProvider(
            model=settings.llm_model,
            base_url=settings.ollama_base_url,
        )
    return NoopLLMProvider()


def build_llm_provider(settings: Any) -> LLMProvider:
    """Factory: build the configured LLM provider (resilient + concurrency-capped).

    Order: CappedLLMProvider (G9) wraps ResilientLLMProvider (G10) wraps the
    HTTP provider. Noop skips the resilience wrapper so disabled mode is unchanged.
    """
    inner = _build_inner_llm_provider(settings)
    if not isinstance(inner, NoopLLMProvider):
        inner = ResilientLLMProvider(inner, settings)
    return CappedLLMProvider(inner)
