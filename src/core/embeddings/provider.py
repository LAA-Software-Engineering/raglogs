"""Embeddings provider abstraction.

Mirrors ``src.core.llm.provider``: callers never talk to OpenAI or
sentence-transformers directly. The default ``disabled`` provider is
always available so clustering stays deterministic without an API key.
"""

from typing import Protocol, runtime_checkable

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import get_settings
from src.config.settings import Settings

log = structlog.get_logger()

# OpenAI allows large batches; keep requests modest for local-compatible APIs.
_OPENAI_BATCH_SIZE = 96

# The persisted pgvector column is fixed at this width (migration 0001,
# ``log_embeddings.embedding`` / ``cluster_embeddings.embedding``). Any provider
# that emits a different dimension cannot be stored, so we refuse it loudly
# rather than silently dropping every vector.
STORED_EMBEDDING_DIMS = 1536


class EmbeddingsConfigError(RuntimeError):
    """A configured embeddings provider cannot possibly persist vectors.

    Raised loudly (at startup / before ingest) instead of letting the storage
    layer silently skip every write — see issue #84.
    """


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
    """Optional local encoder.

    Construction raises ``ImportError`` if the extra is missing and
    ``EmbeddingsConfigError`` if the model's native embedding width does not
    match the stored ``Vector`` column — the common case, since the popular
    local models (``all-MiniLM-L6-v2`` = 384, ``all-mpnet-base-v2`` = 768) do
    not emit 1536 dims and would otherwise be dropped silently at persist time.
    """

    def __init__(self, model: str, expected_dimensions: int = STORED_EMBEDDING_DIMS) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is required for EMBEDDINGS_PROVIDER=local. "
                "Install with: pip install 'raglogs[local-embeddings]'"
            ) from exc
        self._model = SentenceTransformer(model)
        actual = self._model.get_sentence_embedding_dimension()
        if actual != expected_dimensions:
            raise EmbeddingsConfigError(
                f"EMBEDDINGS_PROVIDER=local model '{model}' emits {actual}-dim vectors, "
                f"but the stored embedding column is Vector({expected_dimensions}). "
                f"Local embeddings only work with a model that outputs exactly "
                f"{expected_dimensions} dims. Choose such a model, or use "
                f"EMBEDDINGS_PROVIDER=openai (text-embedding-3-small = 1536), or "
                f"EMBEDDINGS_PROVIDER=disabled."
            )

    def is_available(self) -> bool:
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(texts, convert_to_numpy=True)
        return [vector.tolist() for vector in vectors]


def build_embeddings_provider(settings: Settings | None = None) -> EmbeddingsProvider:
    """Strict factory: build the configured provider or raise on a broken config.

    Raises ``ImportError`` (local extra missing) or ``EmbeddingsConfigError``
    (local model dimension mismatch). ``openai`` with no key returns the
    disabled provider — that is "not configured yet", not a broken config.
    Use :func:`validate_embeddings_config` at startup and
    :func:`get_embeddings_provider` on the hot path.
    """
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
        return LocalEmbeddingsProvider(
            model=settings.embeddings_model,
            expected_dimensions=STORED_EMBEDDING_DIMS,
        )

    return DisabledEmbeddingsProvider()


def get_embeddings_provider(settings: Settings | None = None) -> EmbeddingsProvider:
    """Hot-path factory: fail-open to disabled so a misconfig never crashes ingest.

    A broken configuration is logged at ``error`` (never silently swallowed) and
    degrades to the disabled provider. Startup calls
    :func:`validate_embeddings_config` first, so in practice this path only sees
    a healthy config or an already-surfaced one.
    """
    try:
        return build_embeddings_provider(settings)
    except (ImportError, EmbeddingsConfigError) as exc:
        log.error("embeddings_provider_disabled", reason=str(exc))
        return DisabledEmbeddingsProvider()


def validate_embeddings_config(settings: Settings | None = None) -> None:
    """Fail loudly if the embeddings configuration can never persist a vector.

    Call at startup / before ingest. Raises :class:`EmbeddingsConfigError` when
    a non-disabled provider is configured with a dimension the stored column
    cannot hold, or (for ``local``) a model whose native width does not match.
    ``disabled`` is always valid. See issue #84.
    """
    if settings is None:
        settings = get_settings()

    if settings.embeddings_provider == "disabled":
        return

    if settings.embeddings_dimensions != STORED_EMBEDDING_DIMS:
        raise EmbeddingsConfigError(
            f"EMBEDDINGS_DIMENSIONS={settings.embeddings_dimensions} but the stored "
            f"embedding column is Vector({STORED_EMBEDDING_DIMS}). No embeddings can be "
            f"persisted with this setting. Set EMBEDDINGS_DIMENSIONS={STORED_EMBEDDING_DIMS} "
            f"(e.g. text-embedding-3-small) or EMBEDDINGS_PROVIDER=disabled."
        )

    if settings.embeddings_provider == "local":
        # Constructing loads the model and verifies its real output dimension.
        try:
            build_embeddings_provider(settings)
        except ImportError as exc:
            raise EmbeddingsConfigError(str(exc)) from exc
