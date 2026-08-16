"""
Tests for src.adapters.base / src.adapters.file.adapter / src.adapters.cloudwatch.adapter
/ src.adapters.k8s.adapter / src.adapters.registry.

File adapter tests check that the new SourceAdapter-conforming class produces identical
output to the pre-existing free functions (discover_files/detect_format/read_lines).
CloudWatch adapter tests mock the boto3 client boundary, not internals — same convention
as tests/unit/test_worker.py.
Kubernetes export tests use tmp_path files and in-memory tarballs — no cluster required.
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
        from src.adapters.file.adapter import (
            FileSourceAdapter,
            detect_format,
            discover_files,
        )

        (tmp_path / "a.json").write_text('{"message": "x"}\n')
        (tmp_path / "b.log").write_text("plain text\n")

        spec = SourceSpec(
            adapter="file", params={"paths": [str(tmp_path)], "recursive": False}
        )
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

    def test_discover_splits_comma_separated_log_groups_string(self):
        """
        Regression test: a raw API/worker payload can hand log_groups in as a plain
        string (e.g. "/a,/b") rather than a list. Iterating a string yields characters,
        not log group names — discover() must normalize this itself so every caller is
        safe, not just the CLI (which parses --param into a list already).
        """
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        spec = SourceSpec(adapter="cloudwatch", params={"log_groups": "/a,/b"})
        refs = list(adapter.discover(spec))

        assert [r.stream_id for r in refs] == ["/a", "/b"]

    def test_discover_single_log_group_param(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        adapter = CloudWatchSourceAdapter()
        spec = SourceSpec(
            adapter="cloudwatch", params={"log_group": "/aws/lambda/my-service"}
        )
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
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        mock_client = MagicMock()
        mock_client.filter_log_events.side_effect = [
            {
                "events": [{"message": "line1"}, {"message": "line2"}],
                "nextToken": "tok1",
            },
            {
                "events": [{"message": "line3"}],
                "nextToken": "tok1",
            },  # same token -> stop
        ]

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        ref = LogStreamRef(
            adapter="cloudwatch",
            stream_id="/aws/lambda/x",
            metadata={"filter_pattern": ""},
        )

        with patch("src.adapters.cloudwatch.adapter._client", return_value=mock_client):
            lines = [raw.text for raw in adapter.read(ref, _WINDOW)]

        assert lines == ["line1", "line2", "line3"]
        assert mock_client.filter_log_events.call_count == 2
        assert ref.cursor is None  # cleared once exhausted

    def test_read_sets_received_at_from_event_timestamp(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        event_ms = 1767225600000  # 2026-01-01T00:00:00Z
        mock_client = MagicMock()
        mock_client.filter_log_events.return_value = {
            "events": [{"message": "line1", "timestamp": event_ms}]
        }

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        ref = LogStreamRef(adapter="cloudwatch", stream_id="/aws/lambda/x")

        with patch("src.adapters.cloudwatch.adapter._client", return_value=mock_client):
            raw_lines = list(adapter.read(ref, _WINDOW))

        assert raw_lines[0].received_at == datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_read_honors_ref_cursor_on_first_request(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        mock_client = MagicMock()
        mock_client.filter_log_events.return_value = {"events": []}

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        ref = LogStreamRef(
            adapter="cloudwatch", stream_id="/aws/lambda/x", cursor="resume-tok"
        )

        with patch("src.adapters.cloudwatch.adapter._client", return_value=mock_client):
            list(adapter.read(ref, _WINDOW))

        assert (
            mock_client.filter_log_events.call_args.kwargs["nextToken"] == "resume-tok"
        )

    def test_discover_honors_region_param_override(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        spec = SourceSpec(
            adapter="cloudwatch", params={"log_group": "/a", "region": "eu-west-1"}
        )
        refs = list(adapter.discover(spec))

        assert refs[0].metadata["region"] == "eu-west-1"

    def test_read_uses_region_from_ref_metadata(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        mock_client = MagicMock()
        mock_client.filter_log_events.return_value = {"events": []}

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        ref = LogStreamRef(
            adapter="cloudwatch", stream_id="/a", metadata={"region": "eu-west-1"}
        )

        with patch(
            "src.adapters.cloudwatch.adapter._client", return_value=mock_client
        ) as mock_get_client:
            list(adapter.read(ref, _WINDOW))

        mock_get_client.assert_called_once_with("eu-west-1")

    def test_retry_skips_non_retryable_client_errors(self):
        from botocore.exceptions import ClientError

        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        self._no_retry_sleep()

        mock_client = MagicMock()
        mock_client.filter_log_events.side_effect = ClientError(
            {
                "Error": {
                    "Code": "ResourceNotFoundException",
                    "Message": "no such log group",
                }
            },
            "FilterLogEvents",
        )

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        ref = LogStreamRef(adapter="cloudwatch", stream_id="/aws/lambda/x")

        with patch("src.adapters.cloudwatch.adapter._client", return_value=mock_client):
            with pytest.raises(AdapterUnavailableError):
                list(adapter.read(ref, _WINDOW))

        # non-retryable — should fail on the first attempt, not burn 3 retries
        assert mock_client.filter_log_events.call_count == 1

    def test_retry_retries_throttling_errors(self):
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

        assert mock_client.filter_log_events.call_count == 3

    def test_read_source_ref_is_log_group(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        mock_client = MagicMock()
        mock_client.filter_log_events.return_value = {"events": [{"message": "line1"}]}

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        ref = LogStreamRef(adapter="cloudwatch", stream_id="/aws/lambda/x")

        with patch("src.adapters.cloudwatch.adapter._client", return_value=mock_client):
            raw_lines = list(adapter.read(ref, _WINDOW))

        assert all(raw.source_ref == "/aws/lambda/x" for raw in raw_lines)

    def test_read_raises_adapter_unavailable_on_client_error(self):
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
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = None

        with patch("boto3.Session", return_value=mock_session):
            with pytest.raises(AdapterUnavailableError):
                adapter.check_available()

    def test_check_available_ok_with_credentials(self):
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

    def test_get_adapter_k8s(self):
        from src.adapters.k8s.adapter import KubernetesExportAdapter
        from src.adapters.registry import get_adapter
        from src.config import get_settings

        adapter = get_adapter("k8s", get_settings())
        assert isinstance(adapter, KubernetesExportAdapter)
        assert isinstance(
            get_adapter("kubernetes", get_settings()), KubernetesExportAdapter
        )

    def test_get_adapter_unknown_raises(self):
        from src.adapters.registry import get_adapter
        from src.config import get_settings

        with pytest.raises(AdapterUnavailableError):
            get_adapter("does-not-exist", get_settings())


# ── KubernetesExportAdapter ───────────────────────────────────────────────────


class TestKubernetesExportAdapter:
    def test_conforms_to_protocol(self):
        from src.adapters.k8s.adapter import KubernetesExportAdapter

        assert isinstance(KubernetesExportAdapter(), SourceAdapter)

    def test_discover_requires_paths(self):
        from src.adapters.k8s.adapter import KubernetesExportAdapter

        with pytest.raises(AdapterUnavailableError):
            list(
                KubernetesExportAdapter().discover(SourceSpec(adapter="k8s", params={}))
            )

    def test_discover_splits_comma_separated_paths_string(self, tmp_path):
        from src.adapters.k8s.adapter import KubernetesExportAdapter

        a = tmp_path / "a.log"
        b = tmp_path / "b.log"
        a.write_text("one\n")
        b.write_text("two\n")

        spec = SourceSpec(adapter="k8s", params={"paths": f"{a},{b}"})
        refs = list(KubernetesExportAdapter().discover(spec))

        assert {r.stream_id for r in refs} == {str(a), str(b)}

    def test_discover_infers_metadata_from_pods_path(self, tmp_path):
        from src.adapters.k8s.adapter import KubernetesExportAdapter

        log_path = (
            tmp_path
            / "pods"
            / "production_billing-worker-abc_01234567-89ab-cdef-0123-456789abcdef"
            / "billing-worker"
            / "0.log"
        )
        log_path.parent.mkdir(parents=True)
        log_path.write_text("line\n")

        spec = SourceSpec(
            adapter="k8s", params={"paths": [str(tmp_path)], "recursive": True}
        )
        refs = list(KubernetesExportAdapter().discover(spec))

        assert len(refs) == 1
        assert refs[0].metadata["namespace"] == "production"
        assert refs[0].metadata["pod"] == "billing-worker-abc"
        assert refs[0].metadata["container"] == "billing-worker"

    def test_read_sets_defaults_from_path_metadata(self, tmp_path):
        from src.adapters.k8s.adapter import KubernetesExportAdapter

        log_path = (
            tmp_path
            / "pods"
            / "production_billing-worker-abc_01234567-89ab-cdef-0123-456789abcdef"
            / "billing-worker"
            / "0.log"
        )
        log_path.parent.mkdir(parents=True)
        log_path.write_text(
            "2026-03-12T22:01:10.123456789Z stdout F ERROR connection timeout\n"
        )

        adapter = KubernetesExportAdapter()
        spec = SourceSpec(adapter="k8s", params={"paths": [str(log_path)]})
        ref = list(adapter.discover(spec))[0]
        raw_lines = list(adapter.read(ref, _WINDOW))

        assert len(raw_lines) == 1
        assert raw_lines[0].default_service == "billing-worker"
        assert raw_lines[0].default_environment == "production"
        assert raw_lines[0].default_host == "billing-worker-abc"
        assert raw_lines[0].source_ref == "production/billing-worker-abc/billing-worker"
        assert raw_lines[0].extra["kubernetes"]["pod"] == "billing-worker-abc"
        assert "stdout F" in raw_lines[0].text

    def test_discover_expands_tarball(self, tmp_path):
        import tarfile

        from src.adapters.k8s.adapter import KubernetesExportAdapter

        member = "pods/staging_api-xyz_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/api/0.log"
        inner = tmp_path / "inner.log"
        inner.write_text("hello from tar\n")
        archive = tmp_path / "export.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(inner, arcname=member)

        spec = SourceSpec(adapter="k8s", params={"paths": [str(archive)]})
        refs = list(KubernetesExportAdapter().discover(spec))

        assert len(refs) == 1
        assert refs[0].metadata["kind"] == "tar"
        assert refs[0].metadata["namespace"] == "staging"
        assert refs[0].metadata["container"] == "api"

        raw_lines = list(KubernetesExportAdapter().read(refs[0], _WINDOW))
        assert [r.text for r in raw_lines] == ["hello from tar"]
        assert raw_lines[0].default_service == "api"

    def test_read_gzip_file(self, tmp_path):
        import gzip

        from src.adapters.k8s.adapter import KubernetesExportAdapter

        gz = tmp_path / "capture.log.gz"
        with gzip.open(gz, "wt", encoding="utf-8") as handle:
            handle.write("gzipped line\n")

        spec = SourceSpec(adapter="k8s", params={"paths": [str(gz)]})
        refs = list(KubernetesExportAdapter().discover(spec))
        raw_lines = list(KubernetesExportAdapter().read(refs[0], _WINDOW))

        assert refs[0].metadata["kind"] == "gzip"
        assert [r.text for r in raw_lines] == ["gzipped line"]

    def test_build_k8s_params_merges_top_level_paths(self):
        from src.adapters.k8s.adapter import build_k8s_params

        merged = build_k8s_params({}, paths=["./a.log"], recursive=True)
        assert merged["paths"] == ["./a.log"]
        assert merged["recursive"] is True

        already = build_k8s_params({"paths": ["./kept.log"]}, paths=["./ignored.log"])
        assert already["paths"] == ["./kept.log"]
