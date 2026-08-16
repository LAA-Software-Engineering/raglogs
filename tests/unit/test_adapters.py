"""
Tests for src.adapters.base / src.adapters.file.adapter / src.adapters.cloudwatch.adapter
/ src.adapters.registry.

File adapter tests check that the new SourceAdapter-conforming class produces identical
output to the pre-existing free functions (discover_files/detect_format/read_lines).
CloudWatch adapter tests mock the boto3 client boundary, not internals — same convention
as tests/unit/test_worker.py.
"""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.adapters.base import LogStreamRef, SourceAdapter, SourceSpec, TimeWindow
from src.core.errors import AdapterUnavailableError

_WINDOW = TimeWindow(
    start=datetime(2026, 1, 1, tzinfo=timezone.utc),
    end=datetime(2026, 1, 2, tzinfo=timezone.utc),
)


# ── FileSourceAdapter ────────────────────────────────────────────────────────

class TestFileSourceAdapter:
    def test_conforms_to_protocol(self):
        from src.adapters.file.adapter import FileSourceAdapter

        assert isinstance(FileSourceAdapter(), SourceAdapter)

    def test_discover_matches_discover_files_and_detect_format(self, tmp_path):
        from src.adapters.file.adapter import FileSourceAdapter, detect_format, discover_files

        (tmp_path / "a.json").write_text('{"message": "x"}\n')
        (tmp_path / "b.log").write_text("plain text\n")

        spec = SourceSpec(adapter="file", params={"paths": [str(tmp_path)], "recursive": False})
        refs = list(FileSourceAdapter().discover(spec))

        expected = discover_files([str(tmp_path)], recursive=False)
        assert {r.stream_id for r in refs} == {str(p) for p in expected}
        for ref in refs:
            assert ref.metadata["format"] == detect_format(Path(ref.stream_id))

    def test_read_matches_read_lines(self, tmp_path):
        from src.adapters.file.adapter import FileSourceAdapter, read_lines

        f = tmp_path / "a.log"
        f.write_text("line1\nline2\n")

        adapter = FileSourceAdapter()
        ref = LogStreamRef(adapter="file", stream_id=str(f))

        lines = [raw.text for raw in adapter.read(ref, _WINDOW)]
        assert lines == list(read_lines(f))
        assert all(raw.source_ref == str(f) for raw in adapter.read(ref, _WINDOW))


# ── CloudWatchSourceAdapter ──────────────────────────────────────────────────

class TestCloudWatchSourceAdapter:
    def test_conforms_to_protocol(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        assert isinstance(CloudWatchSourceAdapter(), SourceAdapter)

    def test_discover_yields_one_ref_per_log_group(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        spec = SourceSpec(
            adapter="cloudwatch",
            params={"log_groups": ["/a", "/b"], "filter_pattern": "?ERROR"},
        )
        refs = list(adapter.discover(spec))

        assert [r.stream_id for r in refs] == ["/a", "/b"]
        assert all(r.metadata["filter_pattern"] == "?ERROR" for r in refs)

    def test_discover_single_log_group_param(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        adapter = CloudWatchSourceAdapter()
        spec = SourceSpec(adapter="cloudwatch", params={"log_group": "/aws/lambda/my-service"})
        refs = list(adapter.discover(spec))

        assert [r.stream_id for r in refs] == ["/aws/lambda/my-service"]

    def test_discover_requires_log_group(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        adapter = CloudWatchSourceAdapter()
        with pytest.raises(AdapterUnavailableError):
            list(adapter.discover(SourceSpec(adapter="cloudwatch", params={})))

    def _no_retry_sleep(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        # Skip tenacity's real exponential backoff so error-path tests run instantly.
        CloudWatchSourceAdapter._filter_log_events.retry.sleep = lambda *_: None

    def test_read_paginates_until_token_repeats(self):
        pytest.importorskip("boto3")
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        mock_client = MagicMock()
        mock_client.filter_log_events.side_effect = [
            {"events": [{"message": "line1"}, {"message": "line2"}], "nextToken": "tok1"},
            {"events": [{"message": "line3"}], "nextToken": "tok1"},  # same token -> stop
        ]

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        ref = LogStreamRef(adapter="cloudwatch", stream_id="/aws/lambda/x", metadata={"filter_pattern": ""})

        with patch("src.adapters.cloudwatch.adapter._client", return_value=mock_client):
            lines = [raw.text for raw in adapter.read(ref, _WINDOW)]

        assert lines == ["line1", "line2", "line3"]
        assert mock_client.filter_log_events.call_count == 2
        assert ref.cursor is None  # cleared once exhausted

    def test_read_source_ref_is_log_group(self):
        pytest.importorskip("boto3")
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        mock_client = MagicMock()
        mock_client.filter_log_events.return_value = {"events": [{"message": "line1"}]}

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        ref = LogStreamRef(adapter="cloudwatch", stream_id="/aws/lambda/x")

        with patch("src.adapters.cloudwatch.adapter._client", return_value=mock_client):
            raw_lines = list(adapter.read(ref, _WINDOW))

        assert all(raw.source_ref == "/aws/lambda/x" for raw in raw_lines)

    def test_read_raises_adapter_unavailable_on_client_error(self):
        pytest.importorskip("boto3")
        from botocore.exceptions import ClientError

        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        self._no_retry_sleep()

        mock_client = MagicMock()
        mock_client.filter_log_events.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "rate exceeded"}},
            "FilterLogEvents",
        )

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        ref = LogStreamRef(adapter="cloudwatch", stream_id="/aws/lambda/x")

        with patch("src.adapters.cloudwatch.adapter._client", return_value=mock_client):
            with pytest.raises(AdapterUnavailableError):
                list(adapter.read(ref, _WINDOW))

    def test_check_available_raises_without_credentials(self):
        pytest.importorskip("boto3")
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = None

        with patch("boto3.Session", return_value=mock_session):
            with pytest.raises(AdapterUnavailableError):
                adapter.check_available()

    def test_check_available_ok_with_credentials(self):
        pytest.importorskip("boto3")
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = object()

        with patch("boto3.Session", return_value=mock_session):
            adapter.check_available()  # does not raise


# ── registry ──────────────────────────────────────────────────────────────────

class TestRegistry:
    def test_get_adapter_file(self):
        from src.adapters.file.adapter import FileSourceAdapter
        from src.adapters.registry import get_adapter
        from src.config import get_settings

        adapter = get_adapter("file", get_settings())
        assert isinstance(adapter, FileSourceAdapter)

    def test_get_adapter_cloudwatch(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter
        from src.adapters.registry import get_adapter
        from src.config import get_settings

        adapter = get_adapter("cloudwatch", get_settings())
        assert isinstance(adapter, CloudWatchSourceAdapter)

    def test_get_adapter_unknown_raises(self):
        from src.adapters.registry import get_adapter
        from src.config import get_settings

        with pytest.raises(AdapterUnavailableError):
            get_adapter("does-not-exist", get_settings())
