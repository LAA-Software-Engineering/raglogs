"""
Tests for src.core.ingestion.service — ingest_from_source (adapter-driven ingestion)
and the ingest_files failure-handling fix (job used to stay "running" forever if an
exception escaped the main loop; it now transitions to "failed" with error_message set).

Uses unittest.mock for the DB session, matching tests/unit/test_worker.py's convention
of mocking the I/O boundary rather than internals.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters.base import LogStreamRef, RawLogLine, SourceSpec, TimeWindow
from src.core.errors import AdapterUnavailableError
from src.core.ingestion.service import ingest_files, ingest_from_source
from src.db.models import IngestionJob

_WINDOW = TimeWindow(
    start=datetime(2026, 1, 1, tzinfo=timezone.utc),
    end=datetime(2026, 1, 2, tzinfo=timezone.utc),
)


def _mock_db(existing_source=None):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = existing_source
    added = []
    db.add.side_effect = lambda obj: added.append(obj)
    db.added = added  # convenience handle for assertions
    return db


class FakeAdapter:
    name = "fake"

    def __init__(self, lines, num_refs=1):
        self._lines = lines
        self._num_refs = num_refs

    def discover(self, spec):
        for i in range(self._num_refs):
            yield LogStreamRef(adapter=self.name, stream_id=f"stream-{i}")

    def read(self, ref, window):
        for text in self._lines:
            yield RawLogLine(text=text, source_ref=ref.stream_id)


class FailingAdapter:
    name = "failing"

    def discover(self, spec):
        yield LogStreamRef(adapter=self.name, stream_id="s1")

    def read(self, ref, window):
        yield RawLogLine(text="line1", source_ref="s1")
        raise RuntimeError("boom")


class UnavailableAdapter:
    name = "unavailable"

    def discover(self, spec):
        yield LogStreamRef(adapter=self.name, stream_id="s1")

    def read(self, ref, window):
        raise AdapterUnavailableError("no creds")
        yield  # pragma: no cover — keeps this a generator function


class PartiallyUnavailableAdapter:
    name = "mixed"

    def discover(self, spec):
        yield LogStreamRef(adapter=self.name, stream_id="s1")
        yield LogStreamRef(adapter=self.name, stream_id="s2")

    def read(self, ref, window):
        if ref.stream_id == "s1":
            yield RawLogLine(text='{"message": "ok", "level": "info"}', source_ref="s1")
        else:
            raise AdapterUnavailableError("throttled")
            yield  # pragma: no cover


def _patch_get_adapter(monkeypatch, adapter):
    monkeypatch.setattr("src.adapters.registry.get_adapter", lambda name, settings: adapter)


class TestIngestFromSource:
    def test_happy_path(self, monkeypatch):
        db = _mock_db()
        lines = ['{"message": "boom", "level": "error", "service": "api"}', "plain text line"]
        _patch_get_adapter(monkeypatch, FakeAdapter(lines))

        spec = SourceSpec(adapter="fake", params={})
        job, stats = ingest_from_source(db=db, spec=spec, window=_WINDOW)

        assert job.status == "completed"
        assert job.source_adapter == "fake"
        assert stats.lines_read == 2
        assert stats.parsed_count == 2
        assert "api" in stats.services_detected

    def test_defaults_window_when_not_given(self, monkeypatch):
        db = _mock_db()
        _patch_get_adapter(monkeypatch, FakeAdapter(["a line"]))

        job, stats = ingest_from_source(db=db, spec=SourceSpec(adapter="fake", params={}))

        assert job.status == "completed"

    def test_no_streams_discovered_raises(self, monkeypatch):
        class EmptyAdapter:
            name = "empty"

            def discover(self, spec):
                return []

        _patch_get_adapter(monkeypatch, EmptyAdapter())
        db = _mock_db()

        with pytest.raises(ValueError):
            ingest_from_source(db=db, spec=SourceSpec(adapter="empty", params={}), window=_WINDOW)

    def test_exception_mid_read_marks_job_failed(self, monkeypatch):
        db = _mock_db()
        _patch_get_adapter(monkeypatch, FailingAdapter())

        with pytest.raises(RuntimeError):
            ingest_from_source(db=db, spec=SourceSpec(adapter="failing", params={}), window=_WINDOW)

        job = next(o for o in db.added if isinstance(o, IngestionJob))
        assert job.status == "failed"
        assert "boom" in job.error_message

    def test_all_streams_unavailable_marks_job_failed_no_partial(self, monkeypatch):
        db = _mock_db()
        _patch_get_adapter(monkeypatch, UnavailableAdapter())

        with pytest.raises(AdapterUnavailableError):
            ingest_from_source(db=db, spec=SourceSpec(adapter="unavailable", params={}), window=_WINDOW)

        job = next(o for o in db.added if isinstance(o, IngestionJob))
        assert job.status == "failed"
        assert "no creds" in job.error_message

    def test_partial_when_one_stream_unavailable(self, monkeypatch):
        db = _mock_db()
        _patch_get_adapter(monkeypatch, PartiallyUnavailableAdapter())

        job, stats = ingest_from_source(db=db, spec=SourceSpec(adapter="mixed", params={}), window=_WINDOW)

        assert job.status == "completed"
        assert job.metadata_json["partial"] is True
        assert stats.lines_read == 1
        assert stats.parsed_count == 1


class TestIngestFiles:
    def test_happy_path_sets_source_adapter(self, tmp_path):
        log_file = tmp_path / "billing-worker.log"
        log_file.write_text('{"message": "ok", "level": "info", "service": "billing"}\n')

        db = _mock_db()
        job, stats = ingest_files(db=db, paths=[str(tmp_path)])

        assert job.status == "completed"
        assert job.source_adapter == "file"
        assert stats.parsed_count == 1

    def test_exception_mid_ingest_marks_job_failed(self, tmp_path):
        """
        Regression test: previously an exception mid-loop left job.status stuck at
        "running" forever because there was no try/except around the main loop.
        """
        log_file = tmp_path / "app.log"
        log_file.write_text('{"message": "hello", "level": "info"}\n')

        db = _mock_db()
        db.bulk_save_objects.side_effect = RuntimeError("db exploded")

        with pytest.raises(RuntimeError):
            ingest_files(db=db, paths=[str(tmp_path)])

        job = next(o for o in db.added if isinstance(o, IngestionJob))
        assert job.status == "failed"
        assert "db exploded" in job.error_message
