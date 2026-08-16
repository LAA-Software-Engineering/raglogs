"""
Tests for src.adapters.base / src.adapters.file.adapter / src.adapters.cloudwatch.adapter
/ src.adapters.loki.adapter / src.adapters.registry.

File adapter tests check that the new SourceAdapter-conforming class produces identical
output to the pre-existing free functions (discover_files/detect_format/read_lines).
CloudWatch adapter tests mock the boto3 client boundary, not internals — same convention
as tests/unit/test_worker.py. Loki adapter tests mock the httpx client boundary.
"""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
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
        ref = LogStreamRef(adapter="cloudwatch", stream_id="/aws/lambda/x", cursor="resume-tok")

        with patch("src.adapters.cloudwatch.adapter._client", return_value=mock_client):
            list(adapter.read(ref, _WINDOW))

        assert mock_client.filter_log_events.call_args.kwargs["nextToken"] == "resume-tok"

    def test_discover_honors_region_param_override(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        spec = SourceSpec(adapter="cloudwatch", params={"log_group": "/a", "region": "eu-west-1"})
        refs = list(adapter.discover(spec))

        assert refs[0].metadata["region"] == "eu-west-1"

    def test_read_uses_region_from_ref_metadata(self):
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        mock_client = MagicMock()
        mock_client.filter_log_events.return_value = {"events": []}

        adapter = CloudWatchSourceAdapter(region="us-east-1")
        ref = LogStreamRef(adapter="cloudwatch", stream_id="/a", metadata={"region": "eu-west-1"})

        with patch("src.adapters.cloudwatch.adapter._client", return_value=mock_client) as mock_get_client:
            list(adapter.read(ref, _WINDOW))

        mock_get_client.assert_called_once_with("eu-west-1")

    def test_retry_skips_non_retryable_client_errors(self):
        from botocore.exceptions import ClientError

        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        self._no_retry_sleep()

        mock_client = MagicMock()
        mock_client.filter_log_events.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no such log group"}},
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


# ── LokiSourceAdapter ─────────────────────────────────────────────────────────

def _loki_streams_payload(streams: list) -> dict:
    return {"status": "success", "data": {"resultType": "streams", "result": streams}}


class TestLokiHelpers:
    def test_query_url_appends_path(self):
        from src.adapters.loki.adapter import _query_url

        assert _query_url("http://loki:3100") == "http://loki:3100/loki/api/v1/query_range"
        assert _query_url("http://loki:3100/") == "http://loki:3100/loki/api/v1/query_range"
        assert _query_url("http://loki:3100/loki/api/v1") == "http://loki:3100/loki/api/v1/query_range"

    def test_format_labels_sorts_keys(self):
        from src.adapters.loki.adapter import _format_labels

        assert _format_labels({"namespace": "prod", "app": "api"}) == '{app="api", namespace="prod"}'


class TestLokiSourceAdapter:
    def test_conforms_to_protocol(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        assert isinstance(LokiSourceAdapter(base_url="http://loki"), SourceAdapter)

    def test_discover_yields_one_ref_per_query(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki:3100")
        spec = SourceSpec(
            adapter="loki",
            params={"queries": ['{app="a"}', '{app="b"}'], "tenant": "team-1"},
        )
        refs = list(adapter.discover(spec))

        assert [r.stream_id for r in refs] == ['{app="a"}', '{app="b"}']
        assert all(r.metadata["tenant"] == "team-1" for r in refs)
        assert all("url" not in r.metadata for r in refs)

    def test_discover_single_query_param(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        spec = SourceSpec(adapter="loki", params={"query": '{job="api"}'})
        refs = list(adapter.discover(spec))

        assert [r.stream_id for r in refs] == ['{job="api"}']

    def test_discover_does_not_split_logql_on_commas(self):
        """LogQL selectors contain commas; treating queries as a CSV would
        fragment `{app="a", env="prod"}` into two invalid streams."""
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        spec = SourceSpec(
            adapter="loki",
            params={"queries": '{app="a", env="prod"}'},
        )
        refs = list(adapter.discover(spec))

        assert [r.stream_id for r in refs] == ['{app="a", env="prod"}']

    def test_discover_falls_back_to_default_query(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki", default_query='{job=~".+"}')
        refs = list(adapter.discover(SourceSpec(adapter="loki", params={})))

        assert [r.stream_id for r in refs] == ['{job=~".+"}']

    def test_discover_requires_query(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        with pytest.raises(AdapterUnavailableError, match="query"):
            list(adapter.discover(SourceSpec(adapter="loki", params={})))

    def test_discover_requires_url(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter()
        with pytest.raises(AdapterUnavailableError, match="LOKI_URL"):
            list(adapter.discover(SourceSpec(adapter="loki", params={"query": '{app="a"}'})))

    def test_discover_ignores_url_param(self):
        """URL is settings-only so a caller cannot SSRF via params.url with env creds."""
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        spec = SourceSpec(
            adapter="loki",
            params={"query": '{app="a"}', "url": "https://logs.example.com"},
        )
        refs = list(adapter.discover(spec))

        assert "url" not in refs[0].metadata
        assert adapter.base_url == "http://loki"

    def test_discover_caps_limit_at_loki_max(self):
        from src.adapters.loki.adapter import LokiSourceAdapter, PAGE_SIZE

        adapter = LokiSourceAdapter(base_url="http://loki")
        refs = list(adapter.discover(
            SourceSpec(adapter="loki", params={"query": '{app="a"}', "limit": PAGE_SIZE + 1})
        ))

        assert refs[0].metadata["limit"] == PAGE_SIZE

    def test_discover_rejects_non_integer_limit(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        with pytest.raises(AdapterUnavailableError, match="limit"):
            list(adapter.discover(
                SourceSpec(adapter="loki", params={"query": '{app="a"}', "limit": "nope"})
            ))

    def _no_retry_sleep(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        LokiSourceAdapter._query_range.retry.sleep = lambda *_: None

    def test_read_maps_labels_and_timestamp(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        event_ns = 1767225600000000000  # 2026-01-01T00:00:00Z
        adapter = LokiSourceAdapter(base_url="http://loki")
        ref = LogStreamRef(
            adapter="loki",
            stream_id='{app="api"}',
            metadata={"limit": 5000},
        )
        payload = _loki_streams_payload([
            {
                "stream": {"app": "api", "namespace": "prod", "pod": "api-7b9"},
                "values": [[str(event_ns), "line1"]],
            }
        ])

        with patch.object(adapter, "_query_range", return_value=payload):
            raw_lines = list(adapter.read(ref, _WINDOW))

        assert [r.text for r in raw_lines] == ["line1"]
        assert raw_lines[0].source_ref == '{app="api", namespace="prod", pod="api-7b9"}'
        assert raw_lines[0].received_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert raw_lines[0].default_service == "api"
        assert raw_lines[0].default_environment == "prod"
        assert raw_lines[0].default_host == "api-7b9"
        assert ref.cursor is None

    def test_read_maps_job_and_instance_label_fallbacks(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        ref = LogStreamRef(adapter="loki", stream_id='{job="prom"}', metadata={"limit": 5000})
        payload = _loki_streams_payload([
            {
                "stream": {"job": "prom", "env": "staging", "instance": "10.0.0.8:9090"},
                "values": [["1767225600000000000", "up"]],
            }
        ])

        with patch.object(adapter, "_query_range", return_value=payload):
            raw_lines = list(adapter.read(ref, _WINDOW))

        assert raw_lines[0].default_service == "prom"
        assert raw_lines[0].default_environment == "staging"
        assert raw_lines[0].default_host == "10.0.0.8:9090"

    def test_read_paginates_until_page_is_short(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        ref = LogStreamRef(
            adapter="loki",
            stream_id='{app="api"}',
            metadata={"limit": 2},
        )
        # Timestamps must fall inside _WINDOW; otherwise max_ts stays at window.start.
        t1, t2, t3 = "1767225600000000100", "1767225600000000200", "1767225600000000300"
        pages = [
            _loki_streams_payload([
                {"stream": {"app": "api"}, "values": [
                    [t1, "a"],
                    [t2, "b"],
                ]}
            ]),
            _loki_streams_payload([
                {"stream": {"app": "api"}, "values": [
                    [t3, "c"],
                ]}
            ]),
        ]

        with patch.object(adapter, "_query_range", side_effect=pages) as mock_query:
            lines = [raw.text for raw in adapter.read(ref, _WINDOW)]

        assert lines == ["a", "b", "c"]
        assert mock_query.call_count == 2
        second_params = mock_query.call_args_list[1].args[1]
        assert second_params["start"] == "1767225600000000201"  # last ts + 1ns
        assert ref.cursor is None

    def test_read_honors_ref_cursor_on_first_request(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        ref = LogStreamRef(
            adapter="loki",
            stream_id='{app="api"}',
            cursor="999",
            metadata={"limit": 5000},
        )

        with patch.object(adapter, "_query_range", return_value=_loki_streams_payload([])) as mock_query:
            list(adapter.read(ref, _WINDOW))

        assert mock_query.call_args.args[1]["start"] == "999"

    def test_read_rejects_non_integer_cursor(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        ref = LogStreamRef(
            adapter="loki",
            stream_id='{app="api"}',
            cursor="not-a-ns",
            metadata={"limit": 5000},
        )

        with pytest.raises(AdapterUnavailableError, match="cursor"):
            list(adapter.read(ref, _WINDOW))

    def test_read_ignores_url_in_ref_metadata(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        ref = LogStreamRef(
            adapter="loki",
            stream_id='{app="api"}',
            metadata={"url": "https://evil.example", "limit": 5000},
        )
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = _loki_streams_payload([])

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False

        with patch("src.adapters.loki.adapter.httpx.Client", return_value=mock_client):
            list(adapter.read(ref, _WINDOW))

        assert mock_client.get.call_args.args[0] == "http://loki/loki/api/v1/query_range"

    def test_read_sets_cursor_when_page_is_full(self):
        """A full page means more results may exist — persist next start as cursor
        so a mid-run interrupt can resume (ingest_from_source snapshots ref.cursor)."""
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        ref = LogStreamRef(
            adapter="loki",
            stream_id='{app="api"}',
            metadata={"limit": 1},
        )
        # Raise on the second page so we can observe the cursor written after
        # the first (full) page — same moment ingest_from_source would snapshot it.
        pages = [
            _loki_streams_payload([
                {"stream": {"app": "api"}, "values": [["1767225600000000100", "a"]]}
            ]),
            AdapterUnavailableError("loki unavailable"),
        ]

        with patch.object(adapter, "_query_range", side_effect=pages):
            with pytest.raises(AdapterUnavailableError, match="unavailable"):
                list(adapter.read(ref, _WINDOW))

        assert ref.cursor == "1767225600000000101"

    def test_read_sends_bearer_and_tenant_headers(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(
            base_url="http://loki",
            tenant="team-1",
            bearer_token="tok-abc",
        )
        ref = LogStreamRef(
            adapter="loki",
            stream_id='{app="api"}',
            metadata={"tenant": "team-1", "limit": 5000},
        )
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = _loki_streams_payload([])

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False

        with patch("src.adapters.loki.adapter.httpx.Client", return_value=mock_client):
            list(adapter.read(ref, _WINDOW))

        headers = mock_client.get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer tok-abc"
        assert headers["X-Scope-OrgID"] == "team-1"
        assert mock_client.get.call_args.kwargs["auth"] is None
        assert mock_client.get.call_args.args[0] == "http://loki/loki/api/v1/query_range"

    def test_read_uses_basic_auth_when_no_bearer(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(
            base_url="http://loki",
            username="12345",
            password="glc_token",
        )
        ref = LogStreamRef(
            adapter="loki",
            stream_id='{app="api"}',
            metadata={"limit": 5000},
        )
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = _loki_streams_payload([])

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False

        with patch("src.adapters.loki.adapter.httpx.Client", return_value=mock_client):
            list(adapter.read(ref, _WINDOW))

        assert mock_client.get.call_args.kwargs["auth"] == ("12345", "glc_token")
        assert "Authorization" not in mock_client.get.call_args.kwargs["headers"]

    def test_read_raises_on_loki_error_status(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        ref = LogStreamRef(
            adapter="loki",
            stream_id='{app="api"}',
            metadata={"limit": 5000},
        )
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "status": "error",
            "errorType": "bad_data",
            "error": "parse error",
        }

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False

        with patch("src.adapters.loki.adapter.httpx.Client", return_value=mock_client):
            with pytest.raises(AdapterUnavailableError, match="parse error"):
                list(adapter.read(ref, _WINDOW))

    def test_read_rejects_metric_result_type(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        adapter = LokiSourceAdapter(base_url="http://loki")
        ref = LogStreamRef(
            adapter="loki",
            stream_id="rate({app=\"api\"}[1m])",
            metadata={"limit": 5000},
        )
        payload = {"status": "success", "data": {"resultType": "matrix", "result": []}}

        with patch.object(adapter, "_query_range", return_value=payload):
            with pytest.raises(AdapterUnavailableError, match="resultType"):
                list(adapter.read(ref, _WINDOW))

    def test_retry_skips_non_retryable_http_errors(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        self._no_retry_sleep()

        adapter = LokiSourceAdapter(base_url="http://loki")
        ref = LogStreamRef(
            adapter="loki",
            stream_id='{app="api"}',
            metadata={"limit": 5000},
        )
        request = httpx.Request("GET", "http://loki/loki/api/v1/query_range")
        response = httpx.Response(400, text="parse error", request=request)

        mock_client = MagicMock()
        mock_client.get.return_value = response
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False

        with patch("src.adapters.loki.adapter.httpx.Client", return_value=mock_client):
            with pytest.raises(AdapterUnavailableError):
                list(adapter.read(ref, _WINDOW))

        assert mock_client.get.call_count == 1

    def test_retry_retries_throttling_errors(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        self._no_retry_sleep()

        adapter = LokiSourceAdapter(base_url="http://loki")
        ref = LogStreamRef(
            adapter="loki",
            stream_id='{app="api"}',
            metadata={"limit": 5000},
        )
        request = httpx.Request("GET", "http://loki/loki/api/v1/query_range")
        response = httpx.Response(429, text="too many requests", request=request)

        mock_client = MagicMock()
        mock_client.get.return_value = response
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False

        with patch("src.adapters.loki.adapter.httpx.Client", return_value=mock_client):
            with pytest.raises(AdapterUnavailableError):
                list(adapter.read(ref, _WINDOW))

        assert mock_client.get.call_count == 3

    def test_check_available_raises_without_url(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        with pytest.raises(AdapterUnavailableError, match="LOKI_URL"):
            LokiSourceAdapter().check_available()

    def test_check_available_ok_with_url(self):
        from src.adapters.loki.adapter import LokiSourceAdapter

        LokiSourceAdapter(base_url="http://loki").check_available()  # does not raise


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

    def test_get_adapter_loki(self):
        from src.adapters.loki.adapter import LokiSourceAdapter
        from src.adapters.registry import get_adapter
        from src.config import get_settings

        adapter = get_adapter("loki", get_settings())
        assert isinstance(adapter, LokiSourceAdapter)

    def test_get_adapter_unknown_raises(self):
        from src.adapters.registry import get_adapter
        from src.config import get_settings

        with pytest.raises(AdapterUnavailableError):
            get_adapter("does-not-exist", get_settings())

    def test_loki_and_cloudwatch_env_vars_are_unprefixed(self, monkeypatch):
        monkeypatch.setenv("LOKI_URL", "http://loki:3100")
        monkeypatch.setenv("LOKI_QUERY", '{job="api"}')
        monkeypatch.setenv("CLOUDWATCH_REGION", "eu-west-1")
        from src.adapters.loki.adapter import LokiSourceAdapter
        from src.adapters.registry import get_adapter
        from src.config.settings import Settings

        settings = Settings()
        assert settings.loki_url == "http://loki:3100"
        assert settings.loki_query == '{job="api"}'
        assert settings.cloudwatch_region == "eu-west-1"
        adapter = get_adapter("loki", settings)
        assert isinstance(adapter, LokiSourceAdapter)
        assert adapter.base_url == "http://loki:3100"

    def test_core_settings_read_unprefixed_env(self, monkeypatch):
        monkeypatch.setenv("DB_URL", "postgresql+psycopg://x:x@localhost:5432/x")
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        from src.config.settings import Settings

        settings = Settings()
        assert settings.db_url == "postgresql+psycopg://x:x@localhost:5432/x"
        assert settings.llm_provider == "ollama"
