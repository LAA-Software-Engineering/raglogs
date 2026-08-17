"""Unit tests for the embeddings provider factory. No live API calls."""

from unittest.mock import MagicMock, patch

from src.config.settings import Settings
from src.core.embeddings.provider import (
    DisabledEmbeddingsProvider,
    LocalEmbeddingsProvider,
    OpenAIEmbeddingsProvider,
    get_embeddings_provider,
)


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
        def __init__(self, model: str) -> None:
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
