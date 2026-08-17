"""Unit tests for similar-incident search (no database)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.core.embeddings.provider import DisabledEmbeddingsProvider
from src.core.embeddings.store import STORED_EMBEDDING_DIMS
from src.core.retrieval.similar import (
    QueryCluster,
    SimilarMatch,
    SimilarVisibility,
    apply_visibility_filters,
    cluster_embedding_fingerprint_statement,
    cluster_embedding_semantic_statement,
    collect_query_fingerprints,
    find_similar_incidents,
    log_entry_fingerprint_statement,
    query_clusters_from_fingerprints,
    render_similar_summary,
    resolve_similar_visibility,
    search_similar_fingerprint,
)
from src.db.models import ClusterEmbedding

WINDOW_START = datetime(2026, 3, 12, 13, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)
VECTOR = [0.01] * STORED_EMBEDDING_DIMS


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": False})).lower()


def _params(stmt) -> list[object]:
    values: list[object] = []
    for value in stmt.compile(compile_kwargs={"literal_binds": False}).params.values():
        if (
            isinstance(value, (list, tuple))
            and value
            and not isinstance(value[0], float)
        ):
            values.extend(value)
        else:
            values.append(value)
    return values


def _row(
    *,
    scope: str = "incident:INC-1188",
    fingerprint: str = "abc123",
    template: str = "connection refused",
    similarity: float = 0.91,
    count: int = 4,
) -> MagicMock:
    row = MagicMock()
    row.scope = scope
    row.fingerprint = fingerprint
    row.template = template
    row.first_seen = WINDOW_START
    row.last_seen = WINDOW_END
    row.count = count
    row.similarity = similarity
    return row


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


class TestResolveSimilarVisibility:
    def test_auth_off_defaults_to_cross_scope(self) -> None:
        vis = resolve_similar_visibility(
            auth_enabled=False,
            resolved_scope="incident:A",
            cross_scope_requested=None,
        )
        assert vis.cross_scope is True
        assert vis.visible_scope is None

    def test_auth_off_can_pin_same_scope(self) -> None:
        vis = resolve_similar_visibility(
            auth_enabled=False,
            resolved_scope="incident:A",
            cross_scope_requested=False,
        )
        assert vis.cross_scope is False
        assert vis.visible_scope == "incident:A"

    def test_admin_defaults_to_cross_scope(self) -> None:
        vis = resolve_similar_visibility(
            auth_enabled=True,
            resolved_scope="incident:A",
            cross_scope_requested=None,
            role="admin",
        )
        assert vis.cross_scope is True
        assert vis.visible_scope is None

    def test_pinned_query_ignores_cross_scope_request(self) -> None:
        vis = resolve_similar_visibility(
            auth_enabled=True,
            resolved_scope="incident:A",
            cross_scope_requested=True,
            role="query",
            allow_scope_override=False,
        )
        assert vis.cross_scope is False
        assert vis.visible_scope == "incident:A"

    def test_override_query_requires_explicit_cross_scope(self) -> None:
        vis = resolve_similar_visibility(
            auth_enabled=True,
            resolved_scope="incident:A",
            cross_scope_requested=None,
            role="query",
            allow_scope_override=True,
        )
        assert vis.cross_scope is False
        vis_on = resolve_similar_visibility(
            auth_enabled=True,
            resolved_scope="incident:A",
            cross_scope_requested=True,
            role="query",
            allow_scope_override=True,
        )
        assert vis_on.cross_scope is True
        assert vis_on.visible_scope is None


class TestVisibilitySQL:
    def test_pinned_fingerprint_sql_filters_scope(self) -> None:
        vis = SimilarVisibility(cross_scope=False, visible_scope="incident:A")
        stmt = cluster_embedding_fingerprint_statement(
            ["abc123"],
            query_scope="incident:A",
            visibility=vis,
            limit=10,
        )
        sql = _compiled(stmt)
        params = _params(stmt)
        assert "cluster_embeddings" in sql
        assert "scope" in sql
        assert "incident:a" in [str(p).lower() for p in params]
        assert "abc123" in params

    def test_admin_cross_scope_excludes_query_scope(self) -> None:
        vis = SimilarVisibility(cross_scope=True, visible_scope=None)
        stmt = cluster_embedding_fingerprint_statement(
            ["abc123"],
            query_scope="incident:A",
            visibility=vis,
            limit=10,
        )
        sql = _compiled(stmt)
        params = _params(stmt)
        assert "cluster_embeddings.scope" in sql
        assert "incident:A" in params
        assert "!=" in sql or "<>" in sql or " is not " in sql

    def test_semantic_sql_includes_cosine_and_scope(self) -> None:
        vis = SimilarVisibility(cross_scope=False, visible_scope="incident:A")
        stmt = cluster_embedding_semantic_statement(
            VECTOR,
            query_scope="incident:A",
            query_fingerprints=["abc123"],
            visibility=vis,
            min_similarity=0.8,
            limit=10,
        )
        sql = _compiled(stmt)
        where = str(stmt.whereclause).lower()
        compiled = stmt.compile(compile_kwargs={"literal_binds": False})
        assert "cluster_embeddings" in sql
        assert "<=>" in where
        assert 0.8 in compiled.params.values()
        assert "incident:A" in compiled.params.values()
        assert any(p == "abc123" or p == ["abc123"] for p in compiled.params.values())

    def test_log_entry_fallback_sql_includes_scope(self) -> None:
        vis = SimilarVisibility(cross_scope=False, visible_scope="incident:A")
        stmt = log_entry_fingerprint_statement(
            ["abc123"],
            query_scope="incident:A",
            visibility=vis,
            limit=10,
        )
        sql = _compiled(stmt)
        assert "log_entries" in sql
        assert "log_entries.scope" in sql
        assert "incident:A" in _params(stmt)

    def test_apply_visibility_never_omits_pinned_scope(self) -> None:
        vis = SimilarVisibility(cross_scope=False, visible_scope="incident:A")
        from sqlalchemy import select

        stmt = apply_visibility_filters(
            select(ClusterEmbedding),
            query_scope="incident:A",
            query_fingerprints=["fp1"],
            visibility=vis,
            scope_column=ClusterEmbedding.scope,
            fingerprint_column=ClusterEmbedding.fingerprint,
        )
        sql = _compiled(stmt)
        assert "cluster_embeddings.scope" in sql
        assert "incident:A" in _params(stmt)
        assert "fp1" in _params(stmt)


class TestFindSimilarDegradation:
    def test_disabled_provider_uses_fingerprint(self) -> None:
        db = MagicMock()
        db.execute.return_value.all.return_value = [_row()]
        result = find_similar_incidents(
            db,
            [QueryCluster(fingerprint="abc123", template="connection refused")],
            query_scope="incident:A",
            visibility=SimilarVisibility(cross_scope=True, visible_scope=None),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            provider=DisabledEmbeddingsProvider(),
        )
        assert result.retrieval_mode == "fingerprint"
        assert result.matches[0].scope == "incident:INC-1188"
        assert result.matches[0].similarity == 1.0

    def test_provider_down_falls_back_without_raising(self) -> None:
        db = MagicMock()
        db.execute.return_value.all.return_value = [_row()]
        provider = FakeEmbeddingsProvider([], fail=True, available=True)
        result = find_similar_incidents(
            db,
            [QueryCluster(fingerprint="abc123", template="connection refused")],
            query_scope="incident:A",
            visibility=SimilarVisibility(cross_scope=True, visible_scope=None),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            provider=provider,
        )
        assert result.retrieval_mode == "fingerprint"
        assert len(result.matches) == 1

    def test_semantic_path_when_provider_returns_hits(self) -> None:
        db = MagicMock()
        db.execute.return_value.all.return_value = [_row(similarity=0.93)]
        provider = FakeEmbeddingsProvider([VECTOR])
        result = find_similar_incidents(
            db,
            [QueryCluster(fingerprint="abc123", template="connection refused")],
            query_scope="incident:A",
            visibility=SimilarVisibility(cross_scope=True, visible_scope=None),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            provider=provider,
        )
        assert result.retrieval_mode == "semantic"
        assert result.matches[0].similarity == 0.93
        assert provider.calls == [["connection refused"]]

    def test_empty_semantic_falls_back_to_fingerprint(self) -> None:
        db = MagicMock()
        db.execute.return_value.all.side_effect = [
            [],  # semantic
            [_row()],  # fingerprint cluster_embeddings
        ]
        provider = FakeEmbeddingsProvider([VECTOR])
        result = find_similar_incidents(
            db,
            [QueryCluster(fingerprint="abc123", template="connection refused")],
            query_scope="incident:A",
            visibility=SimilarVisibility(cross_scope=True, visible_scope=None),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            provider=provider,
        )
        assert result.retrieval_mode == "fingerprint"
        assert result.matches[0].fingerprint == "abc123"

    def test_db_errors_return_empty_fingerprint_result(self) -> None:
        db = MagicMock()
        db.execute.side_effect = RuntimeError("connection lost")
        result = find_similar_incidents(
            db,
            [QueryCluster(fingerprint="abc123", template="x")],
            query_scope="incident:A",
            provider=DisabledEmbeddingsProvider(),
        )
        assert result.retrieval_mode == "fingerprint"
        assert result.matches == []

    def test_fingerprint_falls_through_to_log_entries(self) -> None:
        db = MagicMock()
        db.execute.return_value.all.side_effect = [[], [_row()]]
        matches = search_similar_fingerprint(
            db,
            [QueryCluster(fingerprint="abc123")],
            query_scope="incident:A",
            visibility=SimilarVisibility(cross_scope=True, visible_scope=None),
            limit=10,
        )
        assert len(matches) == 1
        assert db.execute.call_count == 2
        second_sql = _compiled(db.execute.call_args_list[1][0][0])
        assert "log_entries" in second_sql

    def test_fingerprint_matches_cluster_embeddings_without_log_entries(self) -> None:
        """After raw purge, similar still matches fingerprints on cluster_embeddings."""
        db = MagicMock()
        db.execute.return_value.all.return_value = [_row()]
        matches = search_similar_fingerprint(
            db,
            [QueryCluster(fingerprint="abc123")],
            query_scope="incident:A",
            visibility=SimilarVisibility(cross_scope=True, visible_scope=None),
            limit=10,
        )
        assert len(matches) == 1
        assert matches[0].fingerprint == "abc123"
        assert db.execute.call_count == 1
        sql = _compiled(db.execute.call_args.args[0])
        assert "cluster_embeddings" in sql
        assert "log_entries" not in sql


class TestHelpers:
    def test_collect_query_fingerprints_dedupes(self) -> None:
        assert collect_query_fingerprints("abc", ["abc", "def", "  "]) == ["abc", "def"]

    def test_query_clusters_from_fingerprints(self) -> None:
        clusters = query_clusters_from_fingerprints(["a"], templates={"a": "msg"})
        assert clusters[0].template == "msg"

    def test_render_summary_empty(self) -> None:
        text = render_similar_summary([], "fingerprint")
        assert "No similar" in text

    def test_render_summary_with_match(self) -> None:
        match = SimilarMatch(
            scope="incident:INC-1188",
            fingerprint="abc123",
            template="x",
            similarity=0.94,
            first_seen=None,
            last_seen=None,
            count=1,
        )
        text = render_similar_summary([match], "semantic")
        assert "INC-1188" in text
        assert "abc123" in text


class TestSimilarEndpointPermissions:
    def _key(
        self, role: str, *, scope: str, allow_scope_override: bool = False
    ) -> MagicMock:
        from uuid import uuid4

        record = MagicMock()
        record.id = uuid4()
        record.name = "test"
        record.key_prefix = "rlk_testhash"
        record.role = role
        record.scope = scope
        record.allow_scope_override = allow_scope_override
        record.revoked_at = None
        return record

    def _settings(self):
        from src.config.settings import Settings

        return Settings(_env_file=None, auth_enabled=True, auth_mode="api_key")

    def test_pinned_query_key_forces_same_scope(self) -> None:
        from fastapi.testclient import TestClient

        from src.api.app import app
        from src.core.retrieval.similar import SimilarResult

        result = SimilarResult(
            query_clusters=[QueryCluster(fingerprint="abc123")],
            matches=[],
            retrieval_mode="fingerprint",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        client = TestClient(app, raise_server_exceptions=False)
        with (
            patch("src.config.get_settings", return_value=self._settings()),
            patch(
                "src.api.auth.keys.lookup_api_key",
                return_value=self._key("query", scope="incident:A"),
            ),
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
            patch(
                "src.core.retrieval.similar.find_similar_incidents",
                return_value=result,
            ) as mock_find,
        ):
            resp = client.post(
                "/v1/query/similar",
                json={"fingerprint": "abc123", "cross_scope": True},
                headers={"Authorization": "Bearer rlk_queryrolexx"},
            )

        assert resp.status_code == 200
        vis = mock_find.call_args.kwargs["visibility"]
        assert vis.cross_scope is False
        assert vis.visible_scope == "incident:A"

    def test_admin_key_cross_scope_by_default(self) -> None:
        from fastapi.testclient import TestClient

        from src.api.app import app
        from src.core.retrieval.similar import SimilarResult

        result = SimilarResult(
            query_clusters=[QueryCluster(fingerprint="abc123")],
            matches=[],
            retrieval_mode="fingerprint",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        client = TestClient(app, raise_server_exceptions=False)
        with (
            patch("src.config.get_settings", return_value=self._settings()),
            patch(
                "src.api.auth.keys.lookup_api_key",
                return_value=self._key("admin", scope="incident:A"),
            ),
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
            patch(
                "src.core.retrieval.similar.find_similar_incidents",
                return_value=result,
            ) as mock_find,
        ):
            resp = client.post(
                "/v1/query/similar",
                json={"fingerprint": "abc123"},
                headers={"Authorization": "Bearer rlk_adminrolexx"},
            )

        assert resp.status_code == 200
        vis = mock_find.call_args.kwargs["visibility"]
        assert vis.cross_scope is True
        assert vis.visible_scope is None
