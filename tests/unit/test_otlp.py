"""Unit tests for the OTLP-JSON -> harness converter (#79). No network/cluster."""
import json
from datetime import datetime, timezone

import pytest

from src.eval.otlp import (
    capture_from_otlp_dir,
    load_otlp_file,
    parse_otlp_logs,
    parse_otlp_metrics,
    parse_otlp_traces,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
T0_NANO = str(int(T0.timestamp() * 1_000_000_000))
FAR_NANO = str(int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp() * 1_000_000_000))
WINDOW = (datetime(2026, 1, 1, 11, 55, tzinfo=timezone.utc), datetime(2026, 1, 1, 12, 10, tzinfo=timezone.utc))


def _resource(service):
    return {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]}


class TestLogs:
    def test_maps_fields_and_window(self):
        objs = [{"resourceLogs": [{
            "resource": _resource("payment"),
            "scopeLogs": [{"logRecords": [
                {"timeUnixNano": T0_NANO, "severityText": "ERROR", "body": {"stringValue": "boom"}},
                {"timeUnixNano": FAR_NANO, "severityText": "INFO", "body": {"stringValue": "later"}},
            ]}],
        }]}]
        recs = parse_otlp_logs(objs, WINDOW)
        assert len(recs) == 1  # far-future dropped
        assert recs[0]["service"] == "payment"
        assert recs[0]["message"] == "boom"
        assert recs[0]["level"] == "error"
        assert recs[0]["timestamp"].startswith("2026-01-01T12:00:00")

    def test_level_inferred_when_no_severity(self):
        objs = [{"resourceLogs": [{"resource": _resource("s"), "scopeLogs": [{"logRecords": [
            {"timeUnixNano": T0_NANO, "body": {"stringValue": "NullPointerException here"}},
        ]}]}]}]
        assert parse_otlp_logs(objs)[0]["level"] == "error"


class TestTraces:
    def test_duration_ms_and_fields(self):
        end_nano = str(int(T0.timestamp() * 1e9) + 18_682_000)  # +18.682 ms
        objs = [{"resourceSpans": [{"resource": _resource("adservice"), "scopeSpans": [{"spans": [
            {"traceId": "t1", "spanId": "s1", "parentSpanId": "", "name": "GET /x",
             "startTimeUnixNano": T0_NANO, "endTimeUnixNano": end_nano, "status": {"code": 2}},
        ]}]}]}]
        spans = parse_otlp_traces(objs, WINDOW)
        assert len(spans) == 1
        s = spans[0]
        assert s.service == "adservice" and s.span_id == "s1"
        assert s.parent_span_id is None  # empty string -> None
        assert s.duration_ms == pytest.approx(18.682, rel=1e-4)
        assert s.status_code == "2"

    def test_window_filter(self):
        objs = [{"resourceSpans": [{"resource": _resource("s"), "scopeSpans": [{"spans": [
            {"traceId": "t", "spanId": "a", "startTimeUnixNano": FAR_NANO, "endTimeUnixNano": FAR_NANO},
        ]}]}]}]
        assert parse_otlp_traces(objs, WINDOW) == []


class TestMetrics:
    def test_gauge_and_sum_points(self):
        objs = [{"resourceMetrics": [{"resource": _resource("cart"), "scopeMetrics": [{"metrics": [
            {"name": "cpu", "gauge": {"dataPoints": [{"timeUnixNano": T0_NANO, "asDouble": 0.5}]}},
            {"name": "reqs", "sum": {"dataPoints": [{"timeUnixNano": T0_NANO, "asInt": "42"}]}},
        ]}]}]}]
        samples = parse_otlp_metrics(objs, WINDOW)
        by = {(s.service, s.metric): s.value for s in samples}
        assert by[("cart", "cpu")] == pytest.approx(0.5)
        assert by[("cart", "reqs")] == pytest.approx(42.0)

    def test_captures_instrument_type(self):
        objs = [{"resourceMetrics": [{"resource": _resource("cart"), "scopeMetrics": [{"metrics": [
            {"name": "cpu", "gauge": {"dataPoints": [{"timeUnixNano": T0_NANO, "asDouble": 0.5}]}},
            # cumulative monotonic sum -> counter (default temporality = cumulative)
            {"name": "http_reqs", "sum": {"isMonotonic": True,
                "dataPoints": [{"timeUnixNano": T0_NANO, "asInt": "1000"}]}},
            # delta monotonic sum is already per-interval -> a level, tagged "sum"
            {"name": "http_reqs_delta", "sum": {"isMonotonic": True,
                "aggregationTemporality": "AGGREGATION_TEMPORALITY_DELTA",
                "dataPoints": [{"timeUnixNano": T0_NANO, "asInt": "5"}]}},
            {"name": "queue", "sum": {"isMonotonic": False,
                "dataPoints": [{"timeUnixNano": T0_NANO, "asDouble": 7.0}]}},
        ]}]}]}]
        by = {(s.metric): s for s in parse_otlp_metrics(objs, WINDOW)}
        assert by["cpu"].metric_type == "gauge"
        assert by["http_reqs"].metric_type == "counter" and by["http_reqs"].value == pytest.approx(1000.0)
        assert by["http_reqs_delta"].metric_type == "sum"  # delta = per-interval level, not cumulative
        assert by["queue"].metric_type == "sum"

    def test_histograms_deferred_to_normalization_pr(self):
        # histograms are recognized but not yet ingested (would feed cumulative
        # count into the un-normalized metric arm) — enabled with normalization later
        objs = [{"resourceMetrics": [{"resource": _resource("cart"), "scopeMetrics": [{"metrics": [
            {"name": "latency", "histogram": {"dataPoints": [
                {"timeUnixNano": T0_NANO, "count": "530", "sum": 12.3}]}},
        ]}]}]}]
        assert parse_otlp_metrics(objs, WINDOW) == []


class TestLoadFile:
    def test_jsonl_and_array(self, tmp_path):
        p = tmp_path / "a.json"
        p.write_text('{"x":1}\n{"x":2}\n')
        assert load_otlp_file(p) == [{"x": 1}, {"x": 2}]
        p.write_text(json.dumps([{"x": 3}]))
        assert load_otlp_file(p) == [{"x": 3}]
        assert load_otlp_file(tmp_path / "missing.json") == []


class TestCaptureFromDir:
    def test_reads_three_files(self, tmp_path):
        (tmp_path / "logs.json").write_text(json.dumps({"resourceLogs": [{
            "resource": _resource("payment"),
            "scopeLogs": [{"logRecords": [{"timeUnixNano": T0_NANO, "severityText": "ERROR", "body": {"stringValue": "x"}}]}],
        }]}))
        (tmp_path / "metrics.json").write_text(json.dumps({"resourceMetrics": [{
            "resource": _resource("payment"),
            "scopeMetrics": [{"metrics": [{"name": "cpu", "gauge": {"dataPoints": [{"timeUnixNano": T0_NANO, "asDouble": 1.0}]}}]}],
        }]}))
        # no traces.json -> empty spans, no error
        logs, spans, metrics = capture_from_otlp_dir(tmp_path)(WINDOW[0], WINDOW[1])
        assert len(logs) == 1 and logs[0]["service"] == "payment"
        assert spans == []
        assert len(metrics) == 1
