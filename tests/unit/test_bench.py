"""Unit tests for the benchmark harness pure logic (no Postgres)."""

from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from src.perf.bench import (
    BenchResult,
    QueryCounter,
    format_report,
    generate_jsonl_lines,
)


class TestGenerate:
    def test_line_count(self):
        assert len(generate_jsonl_lines(100)) == 100

    def test_zero_and_negative(self):
        assert generate_jsonl_lines(0) == []
        assert generate_jsonl_lines(-5) == []

    def test_first_line_is_the_deploy_trigger(self):
        lines = generate_jsonl_lines(10)
        assert "Deploy completed" in lines[0]["message"]
        assert lines[0]["service"] == "deployer"

    def test_has_error_and_info_mix(self):
        lines = generate_jsonl_lines(30)
        levels = {line["level"] for line in lines}
        assert "error" in levels
        assert "info" in levels

    def test_deterministic_and_ordered_timestamps(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        a = generate_jsonl_lines(20, base=base)
        b = generate_jsonl_lines(20, base=base)
        assert a == b
        ts = [line["timestamp"] for line in a]
        assert ts == sorted(ts)


class TestQueryCounter:
    def test_counts_within_block_only(self):
        engine = create_engine("sqlite://")
        with QueryCounter(engine) as qc:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                conn.execute(text("SELECT 2"))
        counted = qc.count
        assert counted >= 2
        # After the block the listener is removed, so the count stays put.
        with engine.connect() as conn:
            conn.execute(text("SELECT 3"))
        assert qc.count == counted


class TestResultAndReport:
    def _result(self, explain_seconds: float) -> BenchResult:
        return BenchResult(
            n_lines=1000,
            ingested=1000,
            total_logs=1000,
            ingest_seconds=1.0,
            ingest_queries=5,
            explain_seconds=explain_seconds,
            explain_queries=12,
            target_explain_seconds=10.0,
        )

    def test_meets_target(self):
        assert self._result(3.0).meets_target is True
        assert self._result(20.0).meets_target is False

    def test_to_dict_includes_derived_fields(self):
        d = self._result(3.0).to_dict()
        assert d["meets_target"] is True
        assert "generated_at" in d
        assert d["n_lines"] == 1000

    def test_report_shows_pass_fail(self):
        assert "PASS" in format_report(self._result(3.0))
        assert "FAIL" in format_report(self._result(20.0))
