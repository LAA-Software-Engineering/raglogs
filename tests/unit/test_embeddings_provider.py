"""Unit tests for the embeddings provider factory. No live API calls."""

import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from src.config.settings import Settings
from src.core.embeddings.provider import (
    STORED_EMBEDDING_DIMS,
    DisabledEmbeddingsProvider,
    EmbeddingsConfigError,
    LocalEmbeddingsProvider,
    OpenAIEmbeddingsProvider,
    get_embeddings_provider,
    validate_embeddings_config,
)


@contextmanager
def _fake_sentence_transformers(dim: int):
    """Patch ``sentence_transformers`` with a stub model of a given output dim."""
    module = types.ModuleType("sentence_transformers")

    class _FakeSentenceTransformer:
        def __init__(self, model: str) -> None:
            self._model = model

        def get_sentence_embedding_dimension(self) -> int:
            return dim

        def encode(self, texts, convert_to_numpy=True):
            import numpy as np

            return np.zeros((len(texts), dim))

    module.SentenceTransformer = _FakeSentenceTransformer
    with patch.dict(sys.modules, {"sentence_transformers": module}):
        yield


def test_disabled_is_unavailable() -> None:
    provider = DisabledEmbeddingsProvider()
    assert provider.is_available() is False
    assert provider.embed_texts(["hello"]) == []


def test_factory_disabled_by_default() -> None:
    settings = Settings(_env_file=None)
    provider = get_embeddings_provider(settings)
    assert isinstance(provider, DisabledEmbeddingsProvider)


def test_factory_openai_without_key_is_disabled() -> None:
    settings = Settings(_env_file=None, embeddings_provider="openai", openai_api_key="")
    provider = get_embeddings_provider(settings)
    assert isinstance(provider, DisabledEmbeddingsProvider)


def test_factory_openai_with_key() -> None:
    settings = Settings(
        _env_file=None,
        embeddings_provider="openai",
        openai_api_key="sk-test",
        embeddings_model="text-embedding-3-small",
        embeddings_dimensions=1536,
    )
    provider = get_embeddings_provider(settings)
    assert isinstance(provider, OpenAIEmbeddingsProvider)
    assert provider.is_available() is True
    assert provider.model == "text-embedding-3-small"
    assert provider.dimensions == 1536


def test_factory_local_import_error_is_disabled() -> None:
    settings = Settings(_env_file=None, embeddings_provider="local")

    class Boom:
        def __init__(self, model: str, expected_dimensions: int = 1536) -> None:
            raise ImportError("sentence-transformers missing")

    with patch("src.core.embeddings.provider.LocalEmbeddingsProvider", Boom):
        provider = get_embeddings_provider(settings)
    assert isinstance(provider, DisabledEmbeddingsProvider)


def test_local_provider_missing_extra() -> None:
    import sys

    with patch.dict(sys.modules, {"sentence_transformers": None}):
        try:
            LocalEmbeddingsProvider(model="all-MiniLM-L6-v2")
            raised = False
        except ImportError as exc:
            raised = True
            assert "sentence-transformers" in str(exc)
    assert raised is True


def test_local_provider_dimension_mismatch_raises() -> None:
    # 384-dim model (all-MiniLM-L6-v2) can't fill the Vector(1536) column.
    with _fake_sentence_transformers(dim=384):
        with pytest.raises(EmbeddingsConfigError) as exc:
            LocalEmbeddingsProvider(model="all-MiniLM-L6-v2")
    msg = str(exc.value)
    assert "384" in msg and str(STORED_EMBEDDING_DIMS) in msg


def test_local_provider_matching_dimension_constructs() -> None:
    with _fake_sentence_transformers(dim=STORED_EMBEDDING_DIMS):
        provider = LocalEmbeddingsProvider(model="some-1536-model")
    assert provider.is_available() is True


def test_factory_local_dimension_mismatch_fails_open_to_disabled() -> None:
    # The hot-path factory never raises: it degrades to disabled (logged).
    settings = Settings(_env_file=None, embeddings_provider="local")
    with _fake_sentence_transformers(dim=768):
        provider = get_embeddings_provider(settings)
    assert isinstance(provider, DisabledEmbeddingsProvider)


def test_validate_disabled_is_ok() -> None:
    validate_embeddings_config(Settings(_env_file=None))  # no raise


def test_validate_rejects_non_1536_dimensions() -> None:
    settings = Settings(
        _env_file=None,
        embeddings_provider="openai",
        openai_api_key="sk-test",
        embeddings_dimensions=384,
    )
    with pytest.raises(EmbeddingsConfigError) as exc:
        validate_embeddings_config(settings)
    assert "384" in str(exc.value)


def test_validate_local_dimension_mismatch_raises() -> None:
    settings = Settings(
        _env_file=None,
        embeddings_provider="local",
        embeddings_model="all-mpnet-base-v2",
        embeddings_dimensions=1536,
    )
    with _fake_sentence_transformers(dim=768):
        with pytest.raises(EmbeddingsConfigError):
            validate_embeddings_config(settings)


def test_validate_local_missing_extra_raises() -> None:
    settings = Settings(_env_file=None, embeddings_provider="local")
    with patch.dict(sys.modules, {"sentence_transformers": None}):
        with pytest.raises(EmbeddingsConfigError) as exc:
            validate_embeddings_config(settings)
    assert "sentence-transformers" in str(exc.value)


def test_openai_embed_texts_restores_index_order() -> None:
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
    }
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.post.return_value = fake_response

    provider = OpenAIEmbeddingsProvider(
        api_key="sk-test",
        model="text-embedding-3-small",
        dimensions=2,
    )
    with patch("src.core.embeddings.provider.httpx.Client", return_value=fake_client):
        vectors = provider.embed_texts(["a", "b"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    payload = fake_client.post.call_args.kwargs["json"]
    assert payload["input"] == ["a", "b"]
    assert payload["model"] == "text-embedding-3-small"
    assert payload["dimensions"] == 2
    assert fake_client.post.call_args.args[0].endswith("/embeddings")


def test_openai_embed_texts_empty() -> None:
    provider = OpenAIEmbeddingsProvider(api_key="sk-test", model="m")
    assert provider.embed_texts([]) == []


def test_settings_merge_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.cluster_merge_similarity_threshold == 0.92
    assert settings.cluster_merge_min_count == 1
    assert settings.embeddings_provider == "disabled"
    assert settings.ask_semantic_top_k == 100
    assert settings.ask_semantic_min_similarity == 0.75
