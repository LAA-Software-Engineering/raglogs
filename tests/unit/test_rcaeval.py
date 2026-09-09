"""Unit tests for the RCAEval -> harness case converter (pure; no download)."""

from datetime import datetime, timezone

import pytest

from src.eval.rcaeval import (
    build_case_yaml,
    convert_logs_csv,
    parse_case_dir_name,
    parse_inject_time,
    trigger_type_for_fault,
    window_around,
)


class TestParseCaseName:
    def test_basic(self):
        c = parse_case_dir_name("re2ob_adservice_cpu_1")
        assert (c.suite, c.system, c.service, c.fault, c.instance) == (
            "re2",
            "ob",
            "adservice",
            "cpu",
            "1",
        )

    def test_service_with_underscore(self):
        c = parse_case_dir_name("re3tt_ts_order_service_code_2")
        assert c.suite == "re3"
        assert c.system == "tt"
        assert c.service == "ts_order_service"
        assert c.fault == "code"
        assert c.instance == "2"

    def test_compound_fault_does_not_corrupt_service(self):
        # A two-token fault must be captured whole, not split into the service.
        c = parse_case_dir_name("re2ob_adservice_packet_loss_1")
        assert c.service == "adservice"
        assert c.fault == "packet_loss"
        assert trigger_type_for_fault(c.fault) == "dependency"

    def test_compound_network_delay(self):
        c = parse_case_dir_name("re2ss_carts_network_delay_3")
        assert c.service == "carts"
        assert c.fault == "network_delay"

    def test_unrecognized_fault_falls_back_to_single_token(self):
        c = parse_case_dir_name("re2ob_adservice_weirdfault_1")
        assert c.service == "adservice"
        assert c.fault == "weirdfault"

    def test_rejects_bad_prefix(self):
        with pytest.raises(ValueError):
            parse_case_dir_name("xx1ob_adservice_cpu_1")

    def test_rejects_too_short(self):
        with pytest.raises(ValueError):
            parse_case_dir_name("re2ob_adservice")


class TestFaultType:
    def test_mapping(self):
        assert trigger_type_for_fault("cpu") == "resource"
        assert trigger_type_for_fault("memory") == "resource"
        assert trigger_type_for_fault("delay") == "dependency"
        assert trigger_type_for_fault("packet_loss") == "dependency"
        assert trigger_type_for_fault("code") == "code"
        assert trigger_type_for_fault("mystery") == "resource"  # default


class TestInjectTime:
    def test_epoch_seconds(self):
        dt = parse_inject_time("1700000000")
        assert dt == datetime.fromtimestamp(1700000000, tz=timezone.utc)

    def test_epoch_millis_downscaled(self):
        dt = parse_inject_time("1700000000000")
        assert dt == datetime.fromtimestamp(1700000000, tz=timezone.utc)

    def test_float_and_whitespace(self):
        dt = parse_inject_time("  1700000000.5\n")
        assert dt.timestamp() == pytest.approx(1700000000.5)


class TestConvertLogs:
    def test_with_header(self):
        text = "time,service,message\n1700000000,adservice,Connection refused\n"
        recs = convert_logs_csv(text)
        assert len(recs) == 1
        assert recs[0]["service"] == "adservice"
        assert recs[0]["message"] == "Connection refused"
        assert recs[0]["level"] == "info"
        assert recs[0]["timestamp"].startswith("2023-11-14")

    def test_level_inferred_from_message(self):
        text = "time,service,message\n1700000000,api,NullPointerException in handler\n"
        assert convert_logs_csv(text)[0]["level"] == "error"

    def test_without_header_positional(self):
        text = "1700000000,web,hello world\n"
        recs = convert_logs_csv(text)
        assert len(recs) == 1
        assert recs[0]["service"] == "web"

    def test_skips_unparseable_timestamp(self):
        text = "time,service,message\nnot-a-time,api,boom\n1700000000,api,ok\n"
        recs = convert_logs_csv(text)
        assert len(recs) == 1
        assert recs[0]["message"] == "ok"

    def test_empty(self):
        assert convert_logs_csv("") == []


def _write_parquet(path, rows):
    import pyarrow as pa
    import pyarrow.parquet as pq

    cols = {k: [r[k] for r in rows] for k in rows[0]}
    pq.write_table(pa.table(cols), str(path))


class TestParquetLogs:
    def test_maps_hf_columns_and_filters_window(self, tmp_path):
        from datetime import datetime, timezone

        from src.eval.rcaeval import load_parquet_logs

        # HF schema: timestamp (epoch s), container_name, message.
        rows = [
            {"timestamp": 1700000000, "container_name": "adservice", "message": "started"},
            {"timestamp": 1700000300, "container_name": "adservice", "message": "NullPointer error"},
            {"timestamp": 1700009999, "container_name": "frontend", "message": "way outside window"},
        ]
        p = tmp_path / "logs.parquet"
        _write_parquet(p, rows)

        window = (
            datetime.fromtimestamp(1699999900, tz=timezone.utc),
            datetime.fromtimestamp(1700000400, tz=timezone.utc),
        )
        recs = load_parquet_logs(p, window)
        assert len(recs) == 2  # third row filtered out
        assert recs[0]["service"] == "adservice"
        assert recs[1]["level"] == "error"  # inferred from "error"

    def test_no_window_keeps_all(self, tmp_path):
        from src.eval.rcaeval import load_parquet_logs

        p = tmp_path / "logs.parquet"
        _write_parquet(p, [{"timestamp": 1700000000, "container_name": "s", "message": "m"}])
        assert len(load_parquet_logs(p)) == 1


class TestConvertCaseParquet:
    def test_re3_parquet_case_loads_as_code_trigger(self, tmp_path):
        from src.eval.case import load_case
        from src.eval.rcaeval import convert_case

        src = tmp_path / "re3ob_adservice_f3_1"
        src.mkdir()
        (src / "inject_time.txt").write_text("1700000300")
        _write_parquet(
            src / "logs.parquet",
            [
                {"timestamp": 1700000290, "container_name": "adservice", "message": "ok"},
                {"timestamp": 1700000305, "container_name": "adservice", "message": "boom exception"},
                {"timestamp": 1700099999, "container_name": "frontend", "message": "far future"},
            ],
        )
        out = tmp_path / "out"
        assert convert_case(src, out) is True

        case = load_case(out)
        assert case.root_cause.service == "adservice"
        assert case.trigger.type == "code"  # RE3 = code-level faults
        # the far-future row is outside the window and dropped
        assert sum(1 for _ in open(out / "logs.jsonl")) == 2
        # Incident window starts at injection; baseline covers the pre-injection
        # period so change-ratio has a real comparison (default pre = 300s).
        assert case.window_start == parse_inject_time("1700000300")
        assert case.baseline_window == "300s"


class TestParquetSpans:
    def test_maps_columns_converts_us_to_ms_and_filters_window(self, tmp_path):
        from datetime import datetime, timezone

        from src.eval.rcaeval import load_parquet_spans

        rows = [
            {"traceID": "t1", "spanID": "s1", "parentSpanID": None,
             "serviceName": "adservice", "operationName": "GET /x",
             "startTimeMillis": 1700000000000, "duration": 18682, "statusCode": None},
            {"traceID": "t1", "spanID": "s2", "parentSpanID": "s1",
             "serviceName": "cartservice", "operationName": "GET",
             "startTimeMillis": 1700009999000, "duration": 500, "statusCode": None},
        ]
        p = tmp_path / "traces.parquet"
        _write_parquet(p, rows)
        window = (
            datetime.fromtimestamp(1699999999, tz=timezone.utc),
            datetime.fromtimestamp(1700000100, tz=timezone.utc),
        )
        spans = load_parquet_spans(p, window)
        assert len(spans) == 1  # second row outside window
        s = spans[0]
        assert s.service == "adservice"
        assert s.span_id == "s1"
        assert s.parent_span_id is None
        assert s.duration_ms == pytest.approx(18.682)  # µs -> ms

    def test_no_window_keeps_all(self, tmp_path):
        from src.eval.rcaeval import load_parquet_spans

        p = tmp_path / "traces.parquet"
        _write_parquet(p, [
            {"traceID": "t", "spanID": "a", "parentSpanID": None, "serviceName": "svc",
             "operationName": "op", "startTimeMillis": 1700000000000, "duration": 1000,
             "statusCode": None},
        ])
        assert len(load_parquet_spans(p)) == 1


class TestParquetMetrics:
    def test_melts_wide_columns_and_filters_window(self, tmp_path):
        from datetime import datetime, timezone

        from src.eval.rcaeval import load_parquet_metrics

        rows = [
            {"time": 1700000000, "carts_cpu": 0.5, "carts-db_cpu": 0.1, "front-end_mem": 42.0},
            {"time": 1700009999, "carts_cpu": 0.9, "carts-db_cpu": 0.2, "front-end_mem": 43.0},
        ]
        p = tmp_path / "metrics.parquet"
        _write_parquet(p, rows)
        window = (
            datetime.fromtimestamp(1699999999, tz=timezone.utc),
            datetime.fromtimestamp(1700000100, tz=timezone.utc),
        )
        samples = load_parquet_metrics(p, window)
        # one in-window row × 3 metric columns
        assert len(samples) == 3
        by = {(s.service, s.metric): s.value for s in samples}
        assert by[("carts", "cpu")] == pytest.approx(0.5)
        assert by[("carts-db", "cpu")] == pytest.approx(0.1)
        assert by[("front-end", "mem")] == pytest.approx(42.0)


class TestConvertCaseTelemetry:
    def _case_with_telemetry(self, tmp_path):
        src = tmp_path / "re3ob_adservice_f3_1"
        src.mkdir()
        (src / "inject_time.txt").write_text("1700000300")
        _write_parquet(
            src / "logs.parquet",
            [{"timestamp": 1700000305, "container_name": "adservice", "message": "boom"}],
        )
        _write_parquet(
            src / "traces.parquet",
            [
                {"traceID": "t1", "spanID": "s1", "parentSpanID": None,
                 "serviceName": "adservice", "operationName": "GET /x",
                 "startTimeMillis": 1700000305000, "duration": 18682, "statusCode": None},
                {"traceID": "t1", "spanID": "s2", "parentSpanID": "s1",
                 "serviceName": "cartservice", "operationName": "GET",
                 "startTimeMillis": 1700099999000, "duration": 500, "statusCode": None},
            ],
        )
        _write_parquet(
            src / "metrics.parquet",
            [
                {"time": 1700000305, "adservice_cpu": 0.9, "cartservice_mem": 42.0},
                {"time": 1700099999, "adservice_cpu": 0.1, "cartservice_mem": 43.0},
            ],
        )
        return src

    def test_emits_windowed_sidecars_and_they_round_trip(self, tmp_path):
        from src.eval.rcaeval import convert_case, load_metrics_jsonl, load_spans_jsonl

        out = tmp_path / "out"
        assert convert_case(self._case_with_telemetry(tmp_path), out) is True

        spans = load_spans_jsonl(out / "spans.jsonl")
        assert len(spans) == 1  # far-future span dropped by the window
        assert spans[0].service == "adservice"
        assert spans[0].span_id == "s1"
        assert spans[0].duration_ms == pytest.approx(18.682)  # µs -> ms survived round-trip
        assert spans[0].start_time is not None

        samples = load_metrics_jsonl(out / "metrics.jsonl")
        assert len(samples) == 2  # one in-window row x 2 metric columns
        by = {(s.service, s.metric): s.value for s in samples}
        assert by[("adservice", "cpu")] == pytest.approx(0.9)
        assert by[("cartservice", "mem")] == pytest.approx(42.0)

    def test_logs_only_case_writes_no_sidecars(self, tmp_path):
        from src.eval.rcaeval import convert_case

        src = tmp_path / "re3ss_carts_f1_1"
        src.mkdir()
        (src / "inject_time.txt").write_text("1700000300")
        _write_parquet(
            src / "logs.parquet",
            [{"timestamp": 1700000305, "container_name": "carts", "message": "boom"}],
        )
        out = tmp_path / "out"
        assert convert_case(src, out) is True
        assert not (out / "spans.jsonl").exists()
        assert not (out / "metrics.jsonl").exists()


class TestConvertCase:
    def test_output_loads_as_a_harness_case(self, tmp_path):
        from src.eval.case import load_case
        from src.eval.rcaeval import convert_case

        src = tmp_path / "re2ob_adservice_cpu_1"
        src.mkdir()
        (src / "inject_time.txt").write_text("1700000000\n")
        (src / "logs.csv").write_text(
            "time,service,message\n"
            "1699999950,adservice,started\n"
            "1700000005,adservice,connection refused error\n"
        )
        out = tmp_path / "out"

        assert convert_case(src, out) is True
        assert (out / "logs.jsonl").exists()

        case = load_case(out)
        assert case.id == "re2ob_adservice_cpu_1"
        assert case.root_cause.service == "adservice"
        assert case.trigger.type == "resource"
        assert case.expect_explanation is True
        assert case.logs_paths == [out / "logs.jsonl"]

    def test_missing_files_returns_false(self, tmp_path):
        from src.eval.rcaeval import convert_case

        src = tmp_path / "re2ob_adservice_cpu_1"
        src.mkdir()
        (src / "inject_time.txt").write_text("1700000000\n")
        # no logs.csv
        assert convert_case(src, tmp_path / "out") is False


class TestWindowAndCaseYaml:
    def test_window_around(self):
        t = datetime(2023, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
        start, end = window_around(t, pre_seconds=60, post_seconds=120)
        assert (t - start).total_seconds() == 60
        assert (end - t).total_seconds() == 120

    def test_build_case_yaml(self):
        meta = parse_case_dir_name("re2ob_adservice_cpu_1")
        t = parse_inject_time("1700000000")
        doc = build_case_yaml(meta, t, window_around(t))
        assert doc["id"] == "re2ob_adservice_cpu_1"
        assert doc["root_cause"]["service"] == "adservice"
        assert doc["trigger"]["type"] == "resource"
        assert doc["trigger"]["timestamp"] == t.isoformat()
        assert doc["expect_explanation"] is True
        assert "logs" not in doc  # loader defaults to logs.jsonl in the case dir
