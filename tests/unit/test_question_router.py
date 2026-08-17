"""
Tests for src.core.retrieval.question_router query construction and ask retrieval.

These inspect the compiled SQL of the Select objects passed to db.execute()
rather than hitting a real database — unit tests must not require a DB.
Semantic tests use a mocked embedder; no live embedding API.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.core.embeddings.provider import DisabledEmbeddingsProvider
from src.core.embeddings.store import STORED_EMBEDDING_DIMS, filter_by_min_similarity
from src.core.retrieval.question_router import (
    answer_question,
    fetch_fallback_clusters,
    search_logs,
    search_logs_semantic,
)
from src.db.models import LogEntry

QUESTION = "why are payments being declined?"
WINDOW_START = datetime(2026, 8, 16, 5, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 16, 7, 0, tzinfo=timezone.utc)


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


def _mock_db_returning(rows=None):
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = rows or []
    return db


def _where_clause(mock_db) -> str:
    query = mock_db.execute.call_args[0][0]
    return str(query.whereclause)


def _compiled_sql(mock_db) -> str:
    query = mock_db.execute.call_args[0][0]
    return str(query.compile(compile_kwargs={"literal_binds": False}))


def _entry(message: str, level: str = "error", service: str = "billing") -> LogEntry:
    return LogEntry(
        id=uuid.uuid4(),
        timestamp=WINDOW_START,
        service=service,
        level=level,
        raw_message=message,
        normalized_message=message,
        fingerprint="fp-" + message[:12],
    )


class TestSearchLogsIngestionScoping:
    def test_filters_by_ingestion_job_id_when_given(self):
        db = _mock_db_returning()
        job_id = uuid.uuid4()

        search_logs(db, ["error"], None, None, None, None, ingestion_job_id=job_id)

        sql = _where_clause(db)
        assert "ingestion_job_id" in sql

    def test_no_ingestion_filter_when_omitted(self):
        db = _mock_db_returning()

        search_logs(db, ["error"], None, None, None, None)

        sql = _where_clause(db)
        assert "ingestion_job_id" not in sql


class TestFetchFallbackClustersIngestionScoping:
    def test_filters_by_ingestion_job_id_when_given(self):
        db = _mock_db_returning()
        job_id = uuid.uuid4()

        fetch_fallback_clusters(db, WINDOW_START, WINDOW_END, None, None, ingestion_job_id=job_id)

        sql = _where_clause(db)
        assert "ingestion_job_id" in sql

    def test_no_ingestion_filter_when_omitted(self):
        db = _mock_db_returning()

        fetch_fallback_clusters(db, WINDOW_START, WINDOW_END, None, None)

        sql = _where_clause(db)
        assert "ingestion_job_id" not in sql


class TestSearchLogsSemanticSQL:
    def test_joins_log_embeddings_and_filters(self):
        db = _mock_db_returning()
        job_id = uuid.uuid4()
        vector = [0.01] * STORED_EMBEDDING_DIMS

        search_logs_semantic(
            db,
            vector,
            WINDOW_START,
            WINDOW_END,
            "billing",
            "error",
            limit=50,
            ingestion_job_id=job_id,
            min_similarity=0.75,
        )

        sql = _compiled_sql(db).lower()
        assert "log_embeddings" in sql
        assert "ingestion_job_id" in sql
        assert "service" in sql
        assert QUESTION not in sql
        assert QUESTION not in str(db.execute.call_args)

    def test_window_and_service_filters_without_job(self):
        db = _mock_db_returning()
        vector = [0.02] * STORED_EMBEDDING_DIMS

        search_logs_semantic(
            db,
            vector,
            WINDOW_START,
            WINDOW_END,
            "api",
            None,
        )

        sql = _compiled_sql(db).lower()
        where = _where_clause(db).lower()
        assert "log_embeddings" in sql
        assert "ingestion_job_id" not in where
        assert "timestamp" in sql

    def test_skips_db_when_vector_dimension_mismatches(self):
        db = _mock_db_returning()

        rows = search_logs_semantic(db, [0.1, 0.2], WINDOW_START, WINDOW_END, None, None)

        assert rows == []
        db.execute.assert_not_called()

    def test_question_text_is_not_concatenated_into_sql(self):
        db = _mock_db_returning()
        vector = [0.03] * STORED_EMBEDDING_DIMS

        search_logs_semantic(db, vector, None, None, None, None)

        compiled = db.execute.call_args[0][0].compile()
        blob = str(compiled) + str(compiled.params)
        assert QUESTION not in blob


class TestSimilarityThreshold:
    def test_below_min_similarity_is_excluded(self):
        high = _entry("stripe signature verification failed")
        low = _entry("cache miss on session store")
        kept = filter_by_min_similarity([high, low], [0.91, 0.40], 0.75)
        assert kept == [high]


class TestAnswerQuestionRetrieval:
    def test_disabled_provider_uses_keyword_and_does_not_embed(self):
        keyword_hit = _entry("login failed for user")
        provider = DisabledEmbeddingsProvider()
        db = MagicMock()

        with (
            patch(
                "src.core.retrieval.question_router.get_embeddings_provider",
                return_value=provider,
            ),
            patch(
                "src.core.retrieval.question_router.search_logs",
                return_value=[keyword_hit],
            ) as mock_keyword,
            patch(
                "src.core.retrieval.question_router.search_logs_semantic",
            ) as mock_semantic,
            patch(
                "src.core.llm.provider.build_llm_provider",
            ) as mock_llm,
        ):
            from src.core.llm.provider import NoopLLMProvider

            mock_llm.return_value = NoopLLMProvider()
            result = answer_question(
                db,
                "why did login fail?",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )

        mock_semantic.assert_not_called()
        mock_keyword.assert_called_once()
        assert result.retrieval_mode == "keyword"
        assert result.total_matches == 1

    def test_paraphrase_uses_semantic_hits_not_keyword_set(self):
        semantic_hit = _entry("Stripe signature verification failed for endpoint /webhooks/stripe")
        keyword_hit = _entry("unrelated cache timeout in redis")
        provider = FakeEmbeddingsProvider([[0.1] * STORED_EMBEDDING_DIMS])
        db = MagicMock()

        with (
            patch(
                "src.core.retrieval.question_router.get_embeddings_provider",
                return_value=provider,
            ),
            patch(
                "src.core.retrieval.question_router.search_logs_semantic",
                return_value=[semantic_hit],
            ) as mock_semantic,
            patch(
                "src.core.retrieval.question_router.search_logs",
                return_value=[keyword_hit],
            ) as mock_keyword,
            patch(
                "src.core.llm.provider.build_llm_provider",
            ) as mock_llm,
        ):
            from src.core.llm.provider import NoopLLMProvider

            mock_llm.return_value = NoopLLMProvider()
            result = answer_question(
                db,
                QUESTION,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )

        mock_semantic.assert_called_once()
        mock_keyword.assert_not_called()
        assert provider.calls == [[QUESTION]]
        assert result.retrieval_mode == "semantic"
        assert "Stripe signature verification failed" in result.answer_text
        assert "cache timeout" not in result.answer_text

    def test_semantic_empty_falls_back_to_keyword(self):
        keyword_hit = _entry("login failed for user")
        provider = FakeEmbeddingsProvider([[0.2] * STORED_EMBEDDING_DIMS])
        db = MagicMock()

        with (
            patch(
                "src.core.retrieval.question_router.get_embeddings_provider",
                return_value=provider,
            ),
            patch(
                "src.core.retrieval.question_router.search_logs_semantic",
                return_value=[],
            ),
            patch(
                "src.core.retrieval.question_router.search_logs",
                return_value=[keyword_hit],
            ),
            patch(
                "src.core.llm.provider.build_llm_provider",
            ) as mock_llm,
        ):
            from src.core.llm.provider import NoopLLMProvider

            mock_llm.return_value = NoopLLMProvider()
            result = answer_question(
                db,
                "why did login fail?",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )

        assert result.retrieval_mode == "keyword"
        assert result.total_matches == 1

    def test_provider_exception_falls_back_to_keyword_without_raising(self):
        keyword_hit = _entry("login failed for user")
        provider = FakeEmbeddingsProvider([], fail=True)
        db = MagicMock()

        with (
            patch(
                "src.core.retrieval.question_router.get_embeddings_provider",
                return_value=provider,
            ),
            patch(
                "src.core.retrieval.question_router.search_logs_semantic",
            ) as mock_semantic,
            patch(
                "src.core.retrieval.question_router.search_logs",
                return_value=[keyword_hit],
            ),
            patch(
                "src.core.llm.provider.build_llm_provider",
            ) as mock_llm,
        ):
            from src.core.llm.provider import NoopLLMProvider

            mock_llm.return_value = NoopLLMProvider()
            result = answer_question(
                db,
                "why did login fail?",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )

        mock_semantic.assert_not_called()
        assert result.retrieval_mode == "keyword"
        assert result.total_matches == 1

    def test_keyword_empty_uses_fallback_clusters(self):
        fallback_hit = _entry("auth token invalid")
        provider = DisabledEmbeddingsProvider()
        db = MagicMock()

        with (
            patch(
                "src.core.retrieval.question_router.get_embeddings_provider",
                return_value=provider,
            ),
            patch(
                "src.core.retrieval.question_router.search_logs",
                return_value=[],
            ),
            patch(
                "src.core.retrieval.question_router.fetch_fallback_clusters",
                return_value=[fallback_hit],
            ),
            patch(
                "src.core.llm.provider.build_llm_provider",
            ) as mock_llm,
        ):
            from src.core.llm.provider import NoopLLMProvider

            mock_llm.return_value = NoopLLMProvider()
            result = answer_question(
                db,
                "why did login fail?",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )

        assert result.retrieval_mode == "fallback"
        assert "auth token invalid" in result.answer_text
