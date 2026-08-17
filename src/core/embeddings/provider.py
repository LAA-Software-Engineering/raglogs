"""Embeddings provider abstraction.

Mirrors ``src.core.llm.provider``: callers never talk to OpenAI or
sentence-transformers directly. The default ``disabled`` provider is
always available so clustering stays deterministic without an API key.
"""

from typing import Protocol, runtime_checkable

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import get_settings
from src.config.settings import Settings

# OpenAI allows large batches; keep requests modest for local-compatible APIs.
_OPENAI_BATCH_SIZE = 96


@runtime_checkable
class EmbeddingsProvider(Protocol):
    def is_available(self) -> bool: ...

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class DisabledEmbeddingsProvider:
    """No-op: merge is skipped. Never makes network or model calls."""

    def is_available(self) -> bool:
        return False

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return []


class OpenAIEmbeddingsProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        dimensions: int = 1536,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.dimensions = dimensions

    def is_available(self) -> bool:
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _OPENAI_BATCH_SIZE):
            batch = texts[start : start + _OPENAI_BATCH_SIZE]
            vectors.extend(self._embed_batch(batch))
        return vectors

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, object] = {
            "model": self.model,
            "input": texts,
        }
        if self.dimensions > 0:
            payload["dimensions"] = self.dimensions

        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        # API may return items out of order; restore input order via ``index``.
        items = sorted(data["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in items]


class LocalEmbeddingsProvider:
    """Optional local encoder. Construction raises ImportError if the extra is missing."""

    def __init__(self, model: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is required for EMBEDDINGS_PROVIDER=local. "
                "Install with: pip install 'raglogs[local-embeddings]'"
            ) from exc
        self._model = SentenceTransformer(model)

    def is_available(self) -> bool:
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(texts, convert_to_numpy=True)
        return [vector.tolist() for vector in vectors]


def get_embeddings_provider(settings: Settings | None = None) -> EmbeddingsProvider:
    """Factory: build the configured embeddings provider. Fail-open to disabled."""
    if settings is None:
        settings = get_settings()

    if settings.embeddings_provider == "openai":
        if not settings.openai_api_key:
            return DisabledEmbeddingsProvider()
        return OpenAIEmbeddingsProvider(
            api_key=settings.openai_api_key,
            model=settings.embeddings_model,
            base_url=settings.openai_base_url,
            dimensions=settings.embeddings_dimensions,
        )

    if settings.embeddings_provider == "local":
        try:
            return LocalEmbeddingsProvider(model=settings.embeddings_model)
        except ImportError:
            return DisabledEmbeddingsProvider()

    return DisabledEmbeddingsProvider()
