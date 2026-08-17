"""Unit tests for raw-line hashing and content-dedup upsert."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

from sqlalchemy.dialects import postgresql

from src.core.ingestion.service import (
    IngestionStats,
    _flush_log_batch,
    _process_line,
    log_entry_upsert_statement,
)
from src.core.normalization.fingerprint import fingerprint_message
from src.db.models import LogEntry
from src.utils.hashing import hash_raw_line


def test_hash_raw_line_is_stable_and_distinct_from_fingerprint() -> None:
    line = '{"message": "timeout talking to payments id=abc-123", "level": "error"}'
    first = hash_raw_line(line)
    second = hash_raw_line(line)
    assert first == second
    assert len(first) == 64
    assert first != hash_raw_line(line + " ")
    _normalized, fingerprint = fingerprint_message(
        "timeout talking to payments id=abc-123"
    )
    assert first != fingerprint


def test_process_line_sets_hash_scope_and_empty_source_ref() -> None:
    source = MagicMock()
    source.id = uuid.uuid4()
    job = MagicMock()
    job.id = uuid.uuid4()
    stats = IngestionStats()
    line = '{"message": "hello 99", "level": "info", "service": "api"}'
    entry = _process_line(
        line,
        "json",
        None,
        None,
        source,
        job,
        "file",
        None,
        stats,
    )
    assert entry is not None
    assert entry.original_line_hash == hash_raw_line(line)
    assert entry.scope == "default"
    assert entry.source_ref == ""
    assert entry.fingerprint != entry.original_line_hash
    assert stats.parsed_count == 1


def test_process_line_stamps_caller_scope() -> None:
    source = MagicMock()
    source.id = uuid.uuid4()
    job = MagicMock()
    job.id = uuid.uuid4()
    entry = _process_line(
        "plain info line",
        "text",
        "svc",
        None,
        source,
        job,
        "push",
        "push",
        IngestionStats(),
        scope="incident:INC-9",
    )
    assert entry is not None
    assert entry.scope == "incident:INC-9"
    assert entry.source_ref == "push"
    assert entry.original_line_hash == hash_raw_line("plain info line")


def test_upsert_statement_compiles_on_conflict_do_nothing() -> None:
    entry = LogEntry(
        id=uuid.uuid4(),
        source_adapter="file",
        source_ref="app.log",
        original_line_hash="a" * 64,
        scope="default",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        fingerprint="abcd1234abcd1234",
        raw_message="hello",
    )
    stmt = log_entry_upsert_statement([entry])
    compiled = str(stmt.compile(dialect=postgresql.dialect())).upper()
    assert "ON CONFLICT" in compiled
    assert "DO NOTHING" in compiled
    assert "SCOPE" in compiled
    assert "SOURCE_REF" in compiled
    assert "ORIGINAL_LINE_HASH" in compiled


def test_flush_log_batch_executes_on_conflict_insert() -> None:
    entry = LogEntry(
        id=uuid.uuid4(),
        source_adapter="file",
        source_ref="app.log",
        original_line_hash="b" * 64,
        scope="default",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        fingerprint="ffff",
    )
    db = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = [(entry.id,)]
    db.execute.return_value = result

    inserted, skipped = _flush_log_batch(db, [entry], None)

    assert inserted == 1
    assert skipped == 0
    db.execute.assert_called_once()
    stmt = db.execute.call_args[0][0]
    compiled = str(stmt.compile(dialect=postgresql.dialect())).upper()
    assert "ON CONFLICT" in compiled
    assert "DO NOTHING" in compiled


def test_flush_log_batch_skips_duplicates_when_returning_empty() -> None:
    entry = LogEntry(
        id=uuid.uuid4(),
        source_adapter="file",
        source_ref="app.log",
        original_line_hash="c" * 64,
        scope="default",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = []
    db.execute.return_value = result
    embedder = MagicMock()

    inserted, skipped = _flush_log_batch(db, [entry], embedder)

    assert inserted == 0
    assert skipped == 1
    embedder.embed_texts.assert_not_called()
