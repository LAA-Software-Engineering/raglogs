"""Unit tests for embedding persist helpers. No database, no live embed API."""

import uuid
from unittest.mock import MagicMock

import pytest

from src.config.settings import Settings
from src.core.embeddings.provider import DisabledEmbeddingsProvider
from src.core.embeddings.store import (
    STORED_EMBEDDING_DIMS,
    build_embedding_rows,
    cosine_similarity,
    filter_by_min_similarity,
    ingest_embeddings_provider,
    persist_log_embeddings,
)
from src.db.models import LogEntry


class FakeEmbeddingsProvider:
    def __init__(
        self,
        vectors: list[list[float]],
        *,
        fail: bool = False,
        available: bool = True,
    ) -> None:
        self.vectors = vectors
        self.fail = fail
        self.available = available
        self.calls: list[list[str]] = []

    def is_available(self) -> bool:
        return self.available

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embedding backend down")
        return self.vectors


def _entry(message: str, entry_id: uuid.UUID | None = None) -> LogEntry:
    return LogEntry(
        id=entry_id or uuid.uuid4(),
        raw_message=message,
        normalized_message=message,
    )


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


class TestCosineSimilarity:
    def test_identical_unit_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector_is_zero(self) -> None:
        assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


class TestFilterByMinSimilarity:
    def test_excludes_below_threshold(self) -> None:
        kept = filter_by_min_similarity(["a", "b", "c"], [0.9, 0.5, 0.8], 0.75)
        assert kept == ["a", "c"]

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="does not match"):
            filter_by_min_similarity(["a"], [0.9, 0.1], 0.75)


class TestBuildEmbeddingRows:
    def test_sets_model_name_and_vector(self) -> None:
        entry = _entry("connection refused")
        vector = [0.1] * STORED_EMBEDDING_DIMS
        rows = build_embedding_rows([entry], [vector], model_name="text-embedding-3-small")

        assert len(rows) == 1
        assert rows[0].log_entry_id == entry.id
        assert rows[0].model_name == "text-embedding-3-small"
        assert rows[0].embedding == vector

    def test_skips_wrong_dimension(self) -> None:
        entry = _entry("connection refused")
        rows = build_embedding_rows([entry], [[0.1, 0.2]], model_name="m")
        assert rows == []


class TestIngestEmbeddingsProvider:
    def test_false_flag_returns_none(self) -> None:
        assert ingest_embeddings_provider(False, settings=_settings()) is None

    def test_disabled_provider_returns_none(self) -> None:
        settings = _settings(embeddings_provider="disabled")
        assert ingest_embeddings_provider(True, settings=settings) is None

    def test_dimension_mismatch_returns_none(self) -> None:
        settings = _settings(
            embeddings_provider="openai",
            openai_api_key="sk-test",
            embeddings_dimensions=384,
        )
        assert ingest_embeddings_provider(True, settings=settings) is None


class TestPersistLogEmbeddings:
    def test_mocked_provider_inserts_rows(self) -> None:
        entry = _entry("connection refused")
        vector = [0.05] * STORED_EMBEDDING_DIMS
        provider = FakeEmbeddingsProvider([vector])
        db = MagicMock()
        settings = _settings(embeddings_model="text-embedding-3-small")

        count = persist_log_embeddings(
            db, [entry], provider=provider, settings=settings
        )

        assert count == 1
        db.bulk_save_objects.assert_called_once()
        rows = db.bulk_save_objects.call_args[0][0]
        assert rows[0].log_entry_id == entry.id
        assert rows[0].model_name == "text-embedding-3-small"
        assert rows[0].embedding == vector
        assert provider.calls == [["connection refused"]]

    def test_provider_raise_skips_without_raising(self) -> None:
        entry = _entry("connection refused")
        provider = FakeEmbeddingsProvider([], fail=True)
        db = MagicMock()

        count = persist_log_embeddings(
            db, [entry], provider=provider, settings=_settings()
        )

        assert count == 0
        db.bulk_save_objects.assert_not_called()

    def test_disabled_provider_is_noop(self) -> None:
        entry = _entry("connection refused")
        db = MagicMock()

        count = persist_log_embeddings(
            db,
            [entry],
            provider=DisabledEmbeddingsProvider(),
            settings=_settings(),
        )

        assert count == 0
        db.bulk_save_objects.assert_not_called()

    def test_skips_blank_messages(self) -> None:
        blank = LogEntry(id=uuid.uuid4(), raw_message="  ", normalized_message="")
        provider = FakeEmbeddingsProvider([[0.1] * STORED_EMBEDDING_DIMS])
        db = MagicMock()

        count = persist_log_embeddings(
            db, [blank], provider=provider, settings=_settings()
        )

        assert count == 0
        assert provider.calls == []
        db.bulk_save_objects.assert_not_called()
