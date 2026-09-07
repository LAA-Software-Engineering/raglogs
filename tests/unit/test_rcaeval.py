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
