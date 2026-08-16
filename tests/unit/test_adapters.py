"""
Tests for src.adapters.base / src.adapters.file.adapter / src.adapters.cloudwatch.adapter
/ src.adapters.datadog.adapter / src.adapters.k8s.adapter / src.adapters.registry.

File adapter tests check that the new SourceAdapter-conforming class produces identical
output to the pre-existing free functions (discover_files/detect_format/read_lines).
CloudWatch adapter tests mock the boto3 client boundary, not internals — same convention
as tests/unit/test_worker.py. Datadog adapter tests mock the httpx client boundary.
Kubernetes export tests use tmp_path files and in-memory tarballs — no cluster required.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import orjson
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


# ── DatadogSourceAdapter ─────────────────────────────────────────────────────

def _dd_event(
    message: str = "hello",
    status: str = "error",
    timestamp: str = "2026-01-01T00:00:00.000Z",
    **attrs: object,
) -> dict:
    attributes = {
        "message": message,
        "status": status,
        "timestamp": timestamp,
        **attrs,
    }
    return {"id": "abc", "type": "log", "attributes": attributes}


def _dd_page(events: list, after: str | None = None) -> dict:
    page: dict = {"data": events}
    if after is not None:
        page["meta"] = {"page": {"after": after}}
    return page


def _http_status_error(status_code: int) -> Exception:
    import httpx

    request = httpx.Request("POST", "https://api.datadoghq.com/api/v2/logs/events/search")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


class TestDatadogMapping:
    def test_maps_reserved_attributes_to_json_aliases(self):
        from src.adapters.datadog.adapter import map_datadog_event

        event = _dd_event(
            message="Stripe webhook failed",
            status="error",
            service="billing-worker",
            host="i-abc",
            tags=["env:prod", "version:1.2"],
        )
        mapped = map_datadog_event(event)
        assert mapped["message"] == "Stripe webhook failed"
        assert mapped["level"] == "error"
        assert mapped["service"] == "billing-worker"
        assert mapped["host"] == "i-abc"
        assert mapped["env"] == "prod"
        assert mapped["timestamp"] == "2026-01-01T00:00:00.000Z"

    def test_maps_nested_trace_and_request_ids(self):
        from src.adapters.datadog.adapter import map_datadog_event

        event = _dd_event(
            attributes={
                "dd": {"trace_id": "trace-1"},
                "http": {"request_id": "req-9"},
            }
        )
        mapped = map_datadog_event(event)
        assert mapped["trace_id"] == "trace-1"
        assert mapped["request_id"] == "req-9"

    def test_env_falls_back_to_nested_attribute(self):
        from src.adapters.datadog.adapter import map_datadog_event

        event = _dd_event(attributes={"env": "staging"})
        assert map_datadog_event(event)["env"] == "staging"

    def test_mapped_event_is_parseable_as_json_log_line(self):
        import orjson

        from src.adapters.datadog.adapter import map_datadog_event
        from src.core.parsing.json_parser import parse_json_line

        event = _dd_event(
            message="Stripe webhook failed",
            status="error",
            service="billing-worker",
            host="i-abc",
            tags=["env:prod"],
            attributes={"dd": {"trace_id": "t-1"}, "request_id": "r-1"},
        )
        parsed = parse_json_line(orjson.dumps(map_datadog_event(event)).decode())
        assert parsed is not None
        assert parsed.parse_error is None
        assert parsed.message == "Stripe webhook failed"
        assert parsed.level == "error"
        assert parsed.service == "billing-worker"
        assert parsed.environment == "prod"
        assert parsed.host == "i-abc"
        assert parsed.trace_id == "t-1"
        assert parsed.request_id == "r-1"


class TestDatadogSourceAdapter:
    def test_conforms_to_protocol(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        assert isinstance(DatadogSourceAdapter(api_key="k", app_key="a"), SourceAdapter)

    def test_discover_defaults_query_to_star(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        refs = list(DatadogSourceAdapter().discover(SourceSpec(adapter="datadog", params={})))
        assert [r.stream_id for r in refs] == ["*"]
        assert refs[0].metadata["query"] == "*"

    def test_discover_uses_query_and_indexes(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        spec = SourceSpec(
            adapter="datadog",
            params={"query": "service:api", "indexes": "main,pci"},
        )
        refs = list(DatadogSourceAdapter().discover(spec))
        assert refs[0].stream_id == "main,pci|service:api"
        assert refs[0].metadata["indexes"] == ["main", "pci"]

    def test_discover_clamps_page_size_and_coerces_string_ints(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        spec = SourceSpec(
            adapter="datadog",
            params={"page_size": "5000", "max_rows": "250"},
        )
        refs = list(DatadogSourceAdapter().discover(spec))
        assert refs[0].metadata["page_size"] == 1000
        assert refs[0].metadata["max_rows"] == 250

    def test_api_base_url_variants(self):
        from src.adapters.datadog.adapter import api_base_url

        assert api_base_url("datadoghq.com") == "https://api.datadoghq.com"
        assert api_base_url("us3.datadoghq.com") == "https://api.us3.datadoghq.com"
        assert api_base_url("app.datadoghq.eu") == "https://api.datadoghq.eu"
        assert api_base_url("https://api.datadoghq.com") == "https://api.datadoghq.com"

    def test_api_base_url_rejects_non_datadog_origins(self):
        from src.adapters.datadog.adapter import api_base_url

        for site in (
            "http://evil.example",
            "https://evil.example",
            "https://api.datadoghq.com.evil.example",
            "evil.example",
        ):
            with pytest.raises(AdapterUnavailableError, match="unsupported Datadog site"):
                api_base_url(site)

    def test_discover_rejects_non_datadog_site_param(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        adapter = DatadogSourceAdapter(api_key="k", app_key="a")
        spec = SourceSpec(
            adapter="datadog",
            params={"site": "http://evil.example"},
        )
        with pytest.raises(AdapterUnavailableError, match="unsupported Datadog site"):
            list(adapter.discover(spec))

    def test_constructor_rejects_non_datadog_site(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        with pytest.raises(AdapterUnavailableError, match="unsupported Datadog site"):
            DatadogSourceAdapter(api_key="k", app_key="a", site="https://evil.example")

    def _adapter(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        return DatadogSourceAdapter(api_key="api", app_key="app", site="datadoghq.com")

    def _no_retry_sleep(self) -> None:
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        DatadogSourceAdapter._search_logs.retry.sleep = lambda *_: None

    def test_read_maps_events_and_sets_received_at(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        adapter = self._adapter()
        ref = LogStreamRef(adapter="datadog", stream_id="*", metadata={"query": "*"})
        page = _dd_page([_dd_event(message="line1", timestamp="2026-01-01T00:00:00Z")])

        with patch.object(DatadogSourceAdapter, "_search_logs", return_value=page):
            lines = list(adapter.read(ref, _WINDOW))

        assert len(lines) == 1
        assert '"message":"line1"' in lines[0].text.replace(" ", "")
        assert '"level":"error"' in lines[0].text.replace(" ", "")
        assert lines[0].received_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert lines[0].source_ref == "*"
        assert ref.cursor is None

    def test_read_paginates_until_no_after_cursor(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        adapter = self._adapter()
        ref = LogStreamRef(adapter="datadog", stream_id="service:api", metadata={"query": "service:api"})
        pages = [
            _dd_page([_dd_event(message="a"), _dd_event(message="b")], after="tok1"),
            _dd_page([_dd_event(message="c")]),
        ]

        with patch.object(DatadogSourceAdapter, "_search_logs", side_effect=pages) as mock_search:
            messages = [orjson.loads(raw.text)["message"] for raw in adapter.read(ref, _WINDOW)]

        assert messages == ["a", "b", "c"]
        assert mock_search.call_count == 2
        second_payload = mock_search.call_args_list[1].args[2]
        assert second_payload["page"]["cursor"] == "tok1"
        assert ref.cursor is None

    def test_read_honors_ref_cursor_on_first_request(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        adapter = self._adapter()
        ref = LogStreamRef(
            adapter="datadog",
            stream_id="*",
            cursor="resume-tok",
            metadata={"query": "*"},
        )

        with patch.object(DatadogSourceAdapter, "_search_logs", return_value=_dd_page([])) as mock_search:
            list(adapter.read(ref, _WINDOW))

        payload = mock_search.call_args.args[2]
        assert payload["page"]["cursor"] == "resume-tok"

    def test_read_stops_at_max_rows_and_keeps_next_cursor(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        adapter = self._adapter()
        ref = LogStreamRef(
            adapter="datadog",
            stream_id="*",
            metadata={"query": "*", "max_rows": 2, "page_size": 10},
        )
        page = _dd_page(
            [_dd_event(message="a"), _dd_event(message="b"), _dd_event(message="c")],
            after="next",
        )

        with patch.object(DatadogSourceAdapter, "_search_logs", return_value=page) as mock_search:
            lines = list(adapter.read(ref, _WINDOW))

        assert len(lines) == 2
        assert ref.cursor == "next"
        payload = mock_search.call_args.args[2]
        assert payload["page"]["limit"] == 2  # remaining, not full page_size

    def test_read_sends_absolute_window_and_indexes(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        adapter = self._adapter()
        ref = LogStreamRef(
            adapter="datadog",
            stream_id="main|*",
            metadata={"query": "service:api", "indexes": ["main"], "site": "us3.datadoghq.com"},
        )

        with patch.object(DatadogSourceAdapter, "_search_logs", return_value=_dd_page([])) as mock_search:
            list(adapter.read(ref, _WINDOW))

        url = mock_search.call_args.args[1]
        payload = mock_search.call_args.args[2]
        assert url == "https://api.us3.datadoghq.com/api/v2/logs/events/search"
        assert payload["filter"]["from"] == "2026-01-01T00:00:00.000Z"
        assert payload["filter"]["to"] == "2026-01-02T00:00:00.000Z"
        assert payload["filter"]["query"] == "service:api"
        assert payload["filter"]["indexes"] == ["main"]
        assert payload["sort"] == "timestamp"

    def test_read_raises_without_keys(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        adapter = DatadogSourceAdapter()
        ref = LogStreamRef(adapter="datadog", stream_id="*")
        with pytest.raises(AdapterUnavailableError):
            list(adapter.read(ref, _WINDOW))

    def test_check_available_raises_without_keys(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        with pytest.raises(AdapterUnavailableError):
            DatadogSourceAdapter(api_key="", app_key="").check_available()

    def test_check_available_ok_with_keys(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        DatadogSourceAdapter(api_key="k", app_key="a").check_available()

    def test_retry_skips_non_retryable_client_errors(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        self._no_retry_sleep()
        mock_client = MagicMock()
        mock_client.post.side_effect = _http_status_error(403)
        adapter = self._adapter()
        ref = LogStreamRef(adapter="datadog", stream_id="*", metadata={"query": "*"})

        with patch("src.adapters.datadog.adapter._client", return_value=mock_client):
            with pytest.raises(AdapterUnavailableError):
                list(adapter.read(ref, _WINDOW))

        assert mock_client.post.call_count == 1

    def test_retry_retries_rate_limit_errors(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        self._no_retry_sleep()
        mock_client = MagicMock()
        mock_client.post.side_effect = _http_status_error(429)
        adapter = self._adapter()
        ref = LogStreamRef(adapter="datadog", stream_id="*", metadata={"query": "*"})

        with patch("src.adapters.datadog.adapter._client", return_value=mock_client):
            with pytest.raises(AdapterUnavailableError):
                list(adapter.read(ref, _WINDOW))

        assert mock_client.post.call_count == 3


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

    def test_get_adapter_datadog(self):
        from src.adapters.datadog.adapter import DatadogSourceAdapter
        from src.adapters.registry import get_adapter
        from src.config import get_settings

        adapter = get_adapter("datadog", get_settings())
        assert isinstance(adapter, DatadogSourceAdapter)

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

    def test_discover_infers_metadata_from_gzipped_pods_path(self, tmp_path):
        import gzip

        from src.adapters.k8s.adapter import KubernetesExportAdapter

        log_path = (
            tmp_path
            / "pods"
            / "production_billing-worker-abc_01234567-89ab-cdef-0123-456789abcdef"
            / "billing-worker"
            / "0.log.gz"
        )
        log_path.parent.mkdir(parents=True)
        with gzip.open(log_path, "wt", encoding="utf-8") as handle:
            handle.write("line\n")

        spec = SourceSpec(
            adapter="k8s", params={"paths": [str(tmp_path)], "recursive": True}
        )
        refs = list(KubernetesExportAdapter().discover(spec))

        assert len(refs) == 1
        assert refs[0].metadata["kind"] == "gzip"
        assert refs[0].metadata["namespace"] == "production"
        assert refs[0].metadata["pod"] == "billing-worker-abc"
        assert refs[0].metadata["container"] == "billing-worker"

    def test_read_reassembles_cri_partial_fragments(self, tmp_path):
        from src.adapters.k8s.adapter import KubernetesExportAdapter

        log_path = tmp_path / "0.log"
        log_path.write_text(
            "2026-03-12T22:01:10.123456789Z stdout P ERROR billing-worker Stripe signa\n"
            "2026-03-12T22:01:10.123456790Z stdout F ture verification failed\n"
            "2026-03-12T22:01:11.000000000Z stdout F INFO ready\n"
        )

        adapter = KubernetesExportAdapter()
        spec = SourceSpec(adapter="k8s", params={"paths": [str(log_path)]})
        ref = list(adapter.discover(spec))[0]
        raw_lines = list(adapter.read(ref, _WINDOW))

        assert len(raw_lines) == 2
        assert raw_lines[0].text == (
            "2026-03-12T22:01:10.123456789Z stdout F "
            "ERROR billing-worker Stripe signature verification failed"
        )
        assert raw_lines[1].text == "2026-03-12T22:01:11.000000000Z stdout F INFO ready"

    def test_build_k8s_params_merges_top_level_paths(self):
        from src.adapters.k8s.adapter import build_k8s_params

        merged = build_k8s_params({}, paths=["./a.log"], recursive=True)
        assert merged["paths"] == ["./a.log"]
        assert merged["recursive"] is True

        already = build_k8s_params({"paths": ["./kept.log"]}, paths=["./ignored.log"])
        assert already["paths"] == ["./kept.log"]
