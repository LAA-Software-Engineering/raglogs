"""Unit tests for embedding persist helpers. No database, no live embed API."""

import uuid
from unittest.mock import MagicMock

from src.config.settings import Settings
from src.core.embeddings.provider import DisabledEmbeddingsProvider
from src.core.embeddings.store import (
    STORED_EMBEDDING_DIMS,
    build_embedding_rows,
    cluster_embedding_row_values,
    cluster_embedding_upsert_statement,
    ingest_embeddings_provider,
    persist_cluster_embeddings,
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


class TestBuildEmbeddingRows:
    def test_sets_model_name_and_vector(self) -> None:
        entry = _entry("connection refused")
        vector = [0.1] * STORED_EMBEDDING_DIMS
        rows = build_embedding_rows(
            [entry], [vector], model_name="text-embedding-3-small"
        )

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

    def test_insert_failure_returns_zero_without_raising(self) -> None:
        entry = _entry("connection refused")
        provider = FakeEmbeddingsProvider([[0.05] * STORED_EMBEDDING_DIMS])
        db = MagicMock()
        nested = MagicMock()
        nested.__enter__.return_value = nested
        nested.__exit__.return_value = None
        db.begin_nested.return_value = nested
        db.flush.side_effect = RuntimeError("deadlock")

        count = persist_log_embeddings(
            db, [entry], provider=provider, settings=_settings()
        )

        assert count == 0
        db.begin_nested.assert_called()


class TestClusterEmbeddingUpsert:
    def test_row_values_sets_scope_fingerprint_and_vector(self) -> None:
        vector = [0.2] * STORED_EMBEDDING_DIMS
        row = cluster_embedding_row_values(
            scope="incident:A",
            fingerprint="abc123",
            template="connection refused",
            vector=vector,
            model_name="text-embedding-3-small",
            count=4,
        )
        assert row is not None
        assert row["scope"] == "incident:A"
        assert row["fingerprint"] == "abc123"
        assert row["template"] == "connection refused"
        assert row["embedding"] == vector
        assert row["model_name"] == "text-embedding-3-small"
        assert row["count"] == 4

    def test_row_values_skips_wrong_dimension(self) -> None:
        assert (
            cluster_embedding_row_values(
                scope="default",
                fingerprint="abc",
                template="msg",
                vector=[0.1, 0.2],
                model_name="m",
            )
            is None
        )

    def test_row_values_skips_blank_template(self) -> None:
        assert (
            cluster_embedding_row_values(
                scope="default",
                fingerprint="abc",
                template="  ",
                vector=[0.1] * STORED_EMBEDDING_DIMS,
                model_name="m",
            )
            is None
        )

    def test_upsert_statement_compiles_on_conflict_do_update(self) -> None:
        from sqlalchemy.dialects import postgresql

        vector = [0.05] * STORED_EMBEDDING_DIMS
        row = cluster_embedding_row_values(
            scope="incident:A",
            fingerprint="abc123",
            template="connection refused",
            vector=vector,
            model_name="m",
        )
        assert row is not None
        stmt = cluster_embedding_upsert_statement([row])
        compiled = str(stmt.compile(dialect=postgresql.dialect())).upper()
        assert "ON CONFLICT" in compiled
        assert "DO UPDATE" in compiled
        assert "SCOPE" in compiled
        assert "FINGERPRINT" in compiled
        assert "UX_CLUSTER_EMBEDDINGS_SCOPE_FINGERPRINT" in compiled

    def test_persist_upserts_when_provider_available(self) -> None:
        from src.core.clustering.clusterer import ClusterData

        cluster = ClusterData(
            fingerprint="abc123",
            representative_message="connection refused",
            count=3,
            services={"api": 3},
            levels={"error": 3},
            first_seen=None,
            last_seen=None,
            baseline_count=0,
            change_ratio=1.0,
            importance_score=1.0,
        )
        provider = FakeEmbeddingsProvider([[0.05] * STORED_EMBEDDING_DIMS])
        db = MagicMock()
        settings = _settings(embeddings_model="text-embedding-3-small")

        count = persist_cluster_embeddings(
            db, [cluster], scope="incident:A", provider=provider, settings=settings
        )

        assert count == 1
        db.execute.assert_called_once()
        assert provider.calls == [["connection refused"]]

    def test_persist_disabled_provider_is_noop(self) -> None:
        from src.core.clustering.clusterer import ClusterData

        cluster = ClusterData(
            fingerprint="abc123",
            representative_message="connection refused",
            count=1,
            services={},
            levels={},
            first_seen=None,
            last_seen=None,
            baseline_count=0,
            change_ratio=1.0,
            importance_score=1.0,
        )
        db = MagicMock()
        count = persist_cluster_embeddings(
            db,
            [cluster],
            scope="default",
            provider=DisabledEmbeddingsProvider(),
            settings=_settings(),
        )
        assert count == 0
        db.execute.assert_not_called()

    def test_persist_provider_raise_skips_without_raising(self) -> None:
        from src.core.clustering.clusterer import ClusterData

        cluster = ClusterData(
            fingerprint="abc123",
            representative_message="connection refused",
            count=1,
            services={},
            levels={},
            first_seen=None,
            last_seen=None,
            baseline_count=0,
            change_ratio=1.0,
            importance_score=1.0,
        )
        db = MagicMock()
        count = persist_cluster_embeddings(
            db,
            [cluster],
            scope="default",
            provider=FakeEmbeddingsProvider([], fail=True),
            settings=_settings(),
        )
        assert count == 0
        db.execute.assert_not_called()
