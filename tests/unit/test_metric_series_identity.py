"""#209 M2b — metric series identity through ingestion. Pure, no DB.

The OTLP converter records each datapoint's series identity (datapoint attributes + the reporting
service.instance.id), the row key includes it so two series at one timestamp are two rows, and the
jsonl corpus format round-trips it — while identity-less sources keep their original key and format."""
import json
from datetime import datetime, timezone
from pathlib import Path

from src.core.ingestion.telemetry import ParsedMetricSample, metric_pk
from src.eval.otlp import parse_otlp_metrics
from src.eval.rcaeval import load_metrics_jsonl

_TS = 1_767_268_800_000_000_000  # 2026-01-01T12:00:00Z


def _dp(value, **attrs):
    return {"timeUnixNano": str(_TS), "asDouble": value,
            "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]}


def _otlp(metric, instance="pod-1"):
    resource = [{"key": "service.name", "value": {"stringValue": "host"}}]
    if instance:
        resource.append({"key": "service.instance.id", "value": {"stringValue": instance}})
    return [{"resourceMetrics": [{"resource": {"attributes": resource},
                                  "scopeMetrics": [{"metrics": [metric]}]}]}]


class TestConverterRecordsSeriesIdentity:
    def test_per_mode_gauge_datapoints_are_distinct_series(self):
        metric = {"name": "system.cpu.utilization",
                  "gauge": {"dataPoints": [_dp(0.9, **{"cpu.mode": "idle"}), _dp(0.1, **{"cpu.mode": "user"})]}}
        samples = parse_otlp_metrics(_otlp(metric))
        assert [s.attributes for s in samples] == [
            {"cpu.mode": "idle", "service.instance.id": "pod-1"},
            {"cpu.mode": "user", "service.instance.id": "pod-1"},
        ]
        assert {s.metric_type for s in samples} == {"gauge"}

    def test_attribute_free_datapoint_still_records_identity(self):
        metric = {"name": "jvm.cpu.recent_utilization", "gauge": {"dataPoints": [_dp(0.5)]}}
        (s,) = parse_otlp_metrics(_otlp(metric, instance=None))
        assert s.attributes == {}  # "this datapoint had no attributes" — not None ("never recorded")

    def test_cumulative_monotonic_sum_is_declared_a_counter(self):
        metric = {"name": "jvm.cpu.time",
                  "sum": {"isMonotonic": True, "aggregationTemporality": 2, "dataPoints": [_dp(12.0)]}}
        (s,) = parse_otlp_metrics(_otlp(metric))
        assert s.metric_type == "counter"


class TestRowKey:
    def _m(self, attributes):
        return ParsedMetricSample(service="host", metric="system.cpu.utilization", value=0.5,
                                  ts=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), attributes=attributes)

    def test_two_series_at_one_timestamp_get_two_keys(self):
        assert metric_pk("s", self._m({"cpu.mode": "idle"})) != metric_pk("s", self._m({"cpu.mode": "user"}))

    def test_key_is_independent_of_attribute_order(self):
        assert (metric_pk("s", self._m({"a": "1", "b": "2"}))
                == metric_pk("s", self._m({"b": "2", "a": "1"})))

    def test_identity_less_sources_keep_their_original_key(self):
        import uuid

        from src.core.ingestion.telemetry import _NS
        m = self._m(None)
        assert metric_pk("s", m) == uuid.uuid5(_NS, f"s|{m.service}|{m.metric}|{m.ts}")


class TestJsonlRoundTrip:
    def test_identity_round_trips_and_is_omitted_when_unknown(self, tmp_path: Path):
        from src.eval.rcaeval import _metric_to_jsonl as _metric_to_dict
        ts = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        with_id = ParsedMetricSample(service="h", metric="m", value=1.0, ts=ts, metric_type="gauge",
                                     attributes={"cpu.mode": "user"})
        without = ParsedMetricSample(service="h", metric="m", value=1.0, ts=ts)
        assert "attributes" not in _metric_to_dict(without)
        path = tmp_path / "metrics.jsonl"
        path.write_text("\n".join(json.dumps(_metric_to_dict(m)) for m in (with_id, without)) + "\n")
        a, b = load_metrics_jsonl(path)
        assert a.attributes == {"cpu.mode": "user"} and b.attributes is None
