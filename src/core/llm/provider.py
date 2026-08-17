import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol, runtime_checkable

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def generate_summary(self, evidence_packet: dict) -> str:
        payload = json.dumps(evidence_packet, default=str, indent=2)
        user_message = f"Analyze this incident evidence and produce a summary:\n\n{payload}"

        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 600,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()


class OllamaLLMProvider:
    def __init__(
        self,
        model: str = "llama3",
        base_url: str = "http://localhost:11434",
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
    def generate_summary(self, evidence_packet: dict) -> str:
        payload = json.dumps(evidence_packet, default=str, indent=2)
        prompt = f"{SYSTEM_PROMPT}\n\nIncident evidence:\n{payload}\n\nSummary:"

        with httpx.Client(timeout=120) as client:
            response = client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0},
                },
            )
            response.raise_for_status()
            data = response.json()
            return data.get("response", "").strip()


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
    """

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner

    def generate_summary(self, evidence_packet: dict) -> str:
        skip = isinstance(self.inner, NoopLLMProvider)
        with llm_concurrency_slot(skip=skip):
            return self.inner.generate_summary(evidence_packet)


def unwrap_llm_provider(provider: LLMProvider) -> LLMProvider:
    """Return the inner provider if ``provider`` is concurrency-capped."""
    inner: LLMProvider = provider
    while isinstance(inner, CappedLLMProvider):
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
    if settings.llm_provider == "ollama":
        return OllamaLLMProvider(
            model=settings.llm_model,
            base_url=settings.ollama_base_url,
        )
    return NoopLLMProvider()


def build_llm_provider(settings: Any) -> LLMProvider:
    """Factory: build the configured LLM provider (concurrency-capped)."""
    return CappedLLMProvider(_build_inner_llm_provider(settings))
