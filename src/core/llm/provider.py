import json
from typing import Optional, Protocol, runtime_checkable

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


def build_llm_provider(settings) -> LLMProvider:
    """Factory: build the configured LLM provider."""
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            return NoopLLMProvider()
        return OpenAILLMProvider(
            api_key=settings.openai_api_key,
            model=settings.llm_model,
            base_url=settings.openai_base_url,
        )
    elif settings.llm_provider == "ollama":
        return OllamaLLMProvider(
            model=settings.llm_model,
            base_url=settings.ollama_base_url,
        )
    return NoopLLMProvider()
