"""G13 data retention: cutoffs, per-scope TTL, purge SQL, metrics, scheduler."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.config.settings import Settings
from src.core.retention.policy import (
    apply_scope_override,
    compute_cutoff,
    is_retention_disabled,
    parse_retention_interval,
    resolve_scope_policy,
    validate_policy_intervals,
)
from src.core.retention.purge import (
    PURGE_JOB_TYPE,
    maybe_enqueue_purge,
    purge_raw_chunk,
    purge_scope_batch,
    purge_summary_chunk,
    run_purge,
    select_expired_cluster_embedding_ids,
    select_expired_log_entry_ids,
    should_enqueue_purge,
)
from src.db.models import ClusterEmbedding, LogEntry, WorkerJob
from src.observability.metrics import REGISTRY, record_purge_rows

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()


def _counter(kind: str) -> float:
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if (
                sample.name == "raglogs_purge_rows_total"
                and sample.labels.get("kind") == kind
            ):
                return float(sample.value)
    return 0.0


class ScriptedSession:
    """Minimal Session stand-in: queued execute() results, no database."""

    def __init__(self, script: list[object] | None = None) -> None:
        self.script = list(script or [])
        self.executed: list[object] = []
        self.added: list[object] = []
        self._get_row: object | None = None
        self._get_rows: dict[object, object] = {}

    def execute(self, stmt: object) -> MagicMock:
        self.executed.append(stmt)
        payload = self.script.pop(0) if self.script else []
        result = MagicMock()
        if isinstance(payload, list):
            result.all.return_value = payload
            result.scalar_one.return_value = 0
            result.scalar_one_or_none.return_value = None
        elif isinstance(payload, int):
            result.all.return_value = []
            result.scalar_one.return_value = payload
            result.scalar_one_or_none.return_value = payload
        else:
            result.all.return_value = []
            result.scalar_one.return_value = 0
            result.scalar_one_or_none.return_value = payload
        return result

    def get(self, model: object, key: object) -> object | None:
        if isinstance(getattr(self, "_get_rows", None), dict) and key in self._get_rows:
            return self._get_rows[key]
        return self._get_row

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        return None


class TestParseRetentionInterval:
    def test_duration_days(self) -> None:
        assert parse_retention_interval("30d") == timedelta(days=30)
        assert parse_retention_interval("180d") == timedelta(days=180)

    def test_zero_empty_off_mean_never(self) -> None:
        for value in ("0", "", "off", "OFF", "none", "never", None):
            assert parse_retention_interval(value) is None
            assert is_retention_disabled(value) is True

    def test_zero_duration_means_never(self) -> None:
        assert parse_retention_interval("0d") is None
        assert is_retention_disabled("0h") is True

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_retention_interval("not-a-duration")
        with pytest.raises(ValueError):
            parse_retention_interval("7 days")
        with pytest.raises(ValueError):
            parse_retention_interval("30")
        with pytest.raises(ValueError):
            validate_policy_intervals(
                apply_scope_override(
                    scope="x",
                    default_raw="7 days",
                    default_summary="180d",
                )
            )


class TestComputeCutoff:
    def test_subtracts_interval(self) -> None:
        cutoff = compute_cutoff("30d", now=NOW)
        assert cutoff == NOW - timedelta(days=30)

    def test_skip_when_disabled(self) -> None:
        assert compute_cutoff("0", now=NOW) is None
        assert compute_cutoff("off", now=NOW) is None
        assert compute_cutoff("", now=NOW) is None
        assert compute_cutoff(None, now=NOW) is None


class TestPerScopeOverride:
    def test_missing_override_uses_env_default(self) -> None:
        policy = apply_scope_override(
            scope="incident:A",
            default_raw="30d",
            default_summary="180d",
            override_raw=None,
            override_summary=None,
        )
        assert policy.raw_interval == "30d"
        assert policy.summary_interval == "180d"

    def test_partial_override(self) -> None:
        policy = apply_scope_override(
            scope="incident:A",
            default_raw="30d",
            default_summary="180d",
            override_raw="7d",
            override_summary=None,
        )
        assert policy.raw_interval == "7d"
        assert policy.summary_interval == "180d"

    def test_empty_override_disables_tier(self) -> None:
        policy = apply_scope_override(
            scope="incident:A",
            default_raw="30d",
            default_summary="180d",
            override_raw="0",
            override_summary="off",
        )
        assert compute_cutoff(policy.raw_interval, now=NOW) is None
        assert compute_cutoff(policy.summary_interval, now=NOW) is None

    def test_resolve_scope_policy_reads_table_row(self) -> None:
        db = ScriptedSession()
        row = MagicMock()
        row.raw_interval = "14d"
        row.summary_interval = None
        db._get_row = row
        settings = Settings(
            _env_file=None, retention_raw="30d", retention_summary="180d"
        )
        policy = resolve_scope_policy(db, "incident:prod", settings=settings)
        assert policy.raw_interval == "14d"
        assert policy.summary_interval == "180d"

    def test_resolve_scope_policy_missing_row(self) -> None:
        db = ScriptedSession()
        settings = Settings(
            _env_file=None, retention_raw="30d", retention_summary="180d"
        )
        policy = resolve_scope_policy(db, "default", settings=settings)
        assert policy.raw_interval == "30d"
        assert policy.summary_interval == "180d"


class TestPurgeSql:
    def test_raw_select_filters_scope_and_created_at(self) -> None:
        cutoff = NOW - timedelta(days=30)
        stmt = select_expired_log_entry_ids("incident:A", cutoff, limit=100)
        sql = _compiled(stmt)
        assert "log_entries" in sql
        assert "created_at" in sql
        assert "incident:a" in sql
        assert "cluster_embeddings" not in sql
        assert "limit" in sql
        assert "coalesce" not in sql
        assert "timestamp" not in sql or "created_at" in sql

    def test_raw_select_uses_created_at_not_event_timestamp(self) -> None:
        stmt = select_expired_log_entry_ids("default", NOW, limit=10)
        sql = _compiled(stmt)
        assert "log_entries.created_at" in sql
        assert "log_entries.timestamp" not in sql

    def test_raw_select_does_not_target_cluster_embeddings(self) -> None:
        stmt = select_expired_log_entry_ids("default", NOW, limit=10)
        sql = _compiled(stmt)
        assert LogEntry.__tablename__ in sql
        assert ClusterEmbedding.__tablename__ not in sql

    def test_summary_select_filters_scope(self) -> None:
        stmt = select_expired_cluster_embedding_ids("incident:B", NOW, limit=50)
        sql = _compiled(stmt)
        assert "cluster_embeddings" in sql
        assert "incident:b" in sql
        assert "log_entries" not in sql


class TestPurgeExecution:
    def test_raw_chunk_deletes_log_entries_counts_embeddings(self) -> None:
        entry_id = uuid.uuid4()
        db = ScriptedSession([[(entry_id,)], 2, 0, None])
        counts = purge_raw_chunk(db, "incident:A", NOW, limit=100, dry_run=False)
        assert counts.raw == 1
        assert counts.embedding == 2
        assert counts.summary == 0
        sqls = [_compiled(s) for s in db.executed]
        assert any("log_entries" in s and "delete" in s for s in sqls)
        assert all("cluster_embeddings" not in s or "delete" not in s for s in sqls)

    def test_raw_chunk_dry_run_skips_delete(self) -> None:
        entry_id = uuid.uuid4()
        db = ScriptedSession([[(entry_id,)], 1, 0])
        counts = purge_raw_chunk(db, "default", NOW, limit=10, dry_run=True)
        assert counts.raw == 1
        assert counts.embedding == 1
        assert not any("delete" in _compiled(s) for s in db.executed)

    def test_zero_interval_skips_raw_and_summary(self) -> None:
        policy = apply_scope_override(
            scope="default",
            default_raw="0",
            default_summary="off",
        )
        db = ScriptedSession()
        with (
            patch("src.core.retention.purge.purge_raw_chunk") as raw,
            patch("src.core.retention.purge.purge_summary_chunk") as summary,
        ):
            counts = purge_scope_batch(db, policy, now=NOW, limit=100, dry_run=False)
        raw.assert_not_called()
        summary.assert_not_called()
        assert counts.is_empty()

    def test_summary_chunk_deletes_embeddings_not_log_entries(self) -> None:
        embedding_id = uuid.uuid4()
        explanation_id = uuid.uuid4()
        run_id = uuid.uuid4()
        db = ScriptedSession(
            [
                [(embedding_id,)],
                None,
                [(explanation_id,)],
                None,
                [(run_id,)],
                4,
                None,
            ]
        )
        counts = purge_summary_chunk(db, "incident:A", NOW, limit=50, dry_run=False)
        assert counts.embedding == 1
        assert counts.summary == 1 + 4 + 1  # explanation + clusters + run
        sqls = [_compiled(s) for s in db.executed]
        assert any("cluster_embeddings" in s and "delete" in s for s in sqls)
        assert not any("log_entries" in s and "delete" in s for s in sqls)

    def test_run_purge_one_chunk_per_scope(self) -> None:
        settings = Settings(
            _env_file=None,
            retention_raw="30d",
            retention_summary="0",
            purge_chunk_size=100,
        )
        entry_id = uuid.uuid4()
        db = ScriptedSession(
            [
                [("incident:A",), ("default",)],
                [(entry_id,)],
                0,
                0,
                None,
            ]
        )
        with patch("src.core.retention.purge.write_last_purge_at") as written:
            counts = run_purge(
                db,
                dry_run=False,
                max_chunks=1,
                now=NOW,
                settings=settings,
            )
        written.assert_called_once()
        assert counts.raw >= 1
        assert "incident:A" in counts.scopes or "default" in counts.scopes

    def test_dry_run_max_chunks_none_returns_when_expired_exist(self) -> None:
        """CLI ``--dry-run`` must COUNT once, not loop the same LIMIT ids."""
        settings = Settings(
            _env_file=None,
            retention_raw="30d",
            retention_summary="0",
            purge_chunk_size=1,
        )
        db = ScriptedSession(
            [
                [("default",)],
                5,
                2,
                1,
            ]
        )

        def _fail_loop(*_a: object, **_k: object) -> None:
            raise AssertionError("dry-run must not call write_last_purge_at")

        with patch(
            "src.core.retention.purge.write_last_purge_at", side_effect=_fail_loop
        ):
            counts = run_purge(
                db,
                dry_run=True,
                max_chunks=None,
                now=NOW,
                settings=settings,
            )
        assert counts.raw == 5
        assert counts.embedding == 2
        assert db.script == []
        assert len(db.executed) <= 8

    def test_invalid_policy_skips_scope_and_continues(self) -> None:
        settings = Settings(
            _env_file=None,
            retention_raw="30d",
            retention_summary="0",
            purge_chunk_size=100,
        )
        entry_id = uuid.uuid4()
        db = ScriptedSession(
            [
                [("incident:bad",), ("default",)],
                [(entry_id,)],
                0,
                0,
                None,
            ]
        )
        bad = MagicMock()
        bad.raw_interval = "7 days"
        bad.summary_interval = None
        db._get_rows["incident:bad"] = bad
        with patch("src.core.retention.purge.write_last_purge_at") as written:
            counts = run_purge(
                db,
                dry_run=False,
                max_chunks=1,
                now=NOW,
                settings=settings,
            )
        written.assert_called_once()
        assert counts.raw == 1
        assert "incident:bad" in counts.scopes
        assert "default" in counts.scopes

    def test_skips_last_purge_at_when_more_chunks_remain(self) -> None:
        settings = Settings(
            _env_file=None,
            retention_raw="30d",
            retention_summary="0",
            purge_chunk_size=1,
        )
        entry_id = uuid.uuid4()
        db = ScriptedSession(
            [
                [("default",)],
                [(entry_id,)],
                0,
                0,
                None,
            ]
        )
        with (
            patch("src.core.retention.purge.write_last_purge_at") as written,
            patch("src.core.retention.purge.enqueue_followup_purge") as followup,
        ):
            counts = run_purge(
                db,
                dry_run=False,
                max_chunks=1,
                now=NOW,
                settings=settings,
            )
        written.assert_not_called()
        followup.assert_called_once()
        assert counts.more_remaining is True
        assert counts.raw == 1


class TestPurgeMetrics:
    def test_record_purge_rows_increments(self) -> None:
        before_raw = _counter("raw")
        before_emb = _counter("embedding")
        record_purge_rows(3, kind="raw")
        record_purge_rows(2, kind="embedding")
        assert _counter("raw") == before_raw + 3
        assert _counter("embedding") == before_emb + 2

    def test_raw_chunk_increments_metrics(self) -> None:
        before_raw = _counter("raw")
        before_emb = _counter("embedding")
        entry_id = uuid.uuid4()
        db = ScriptedSession([[(entry_id,)], 5, 3, None])
        purge_raw_chunk(db, "default", NOW, limit=10, dry_run=False)
        assert _counter("raw") == before_raw + 1 + 3
        assert _counter("embedding") == before_emb + 5

    def test_dry_run_does_not_increment_metrics(self) -> None:
        before_raw = _counter("raw")
        entry_id = uuid.uuid4()
        db = ScriptedSession([[(entry_id,)], 1, 0])
        purge_raw_chunk(db, "default", NOW, limit=10, dry_run=True)
        assert _counter("raw") == before_raw


class TestScheduler:
    def test_due_when_never_ran(self) -> None:
        assert (
            should_enqueue_purge(
                last_purge_at=None,
                has_active_purge=False,
                now=NOW,
                interval_seconds=3600,
            )
            is True
        )

    def test_skip_when_recent(self) -> None:
        assert (
            should_enqueue_purge(
                last_purge_at=NOW - timedelta(minutes=10),
                has_active_purge=False,
                now=NOW,
                interval_seconds=3600,
            )
            is False
        )

    def test_due_when_older_than_interval(self) -> None:
        assert (
            should_enqueue_purge(
                last_purge_at=NOW - timedelta(hours=2),
                has_active_purge=False,
                now=NOW,
                interval_seconds=3600,
            )
            is True
        )

    def test_skip_when_active_purge(self) -> None:
        assert (
            should_enqueue_purge(
                last_purge_at=None,
                has_active_purge=True,
                now=NOW,
                interval_seconds=3600,
            )
            is False
        )

    def test_skip_when_interval_zero(self) -> None:
        assert (
            should_enqueue_purge(
                last_purge_at=None,
                has_active_purge=False,
                now=NOW,
                interval_seconds=0,
            )
            is False
        )

    def test_maybe_enqueue_adds_pending_job(self) -> None:
        db = ScriptedSession([None])
        settings = Settings(_env_file=None, purge_interval_seconds=3600)
        assert maybe_enqueue_purge(db, now=NOW, settings=settings) is True
        assert len(db.added) == 1
        job = db.added[0]
        assert isinstance(job, WorkerJob)
        assert job.job_type == PURGE_JOB_TYPE
        assert job.status == "pending"

    def test_maybe_enqueue_skips_when_active(self) -> None:
        db = ScriptedSession([uuid.uuid4()])
        settings = Settings(_env_file=None, purge_interval_seconds=3600)
        assert maybe_enqueue_purge(db, now=NOW, settings=settings) is False
        assert db.added == []
