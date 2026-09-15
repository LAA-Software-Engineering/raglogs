"""Unit tests for the perf-benchmark harness's DB-free parts (#85). No DB."""
import json
from datetime import datetime, timedelta, timezone

from scripts.bench_pipeline import WINDOW_SECONDS, _fmt_table, _gen_lines

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_gen_lines_count_shape_and_window(tmp_path):
    p = tmp_path / "logs.jsonl"
    _gen_lines(5000, p, T0, seed=1)
    lines = p.read_text().splitlines()
    assert len(lines) == 5000
    recs = [json.loads(x) for x in lines]
    assert all(set(r) == {"timestamp", "level", "service", "message"} for r in recs)
    assert all(r["level"] in ("info", "warn", "error") for r in recs)
    # timestamps stay within [T0, T0 + WINDOW_SECONDS]
    end = T0 + timedelta(seconds=WINDOW_SECONDS)
    ts = [datetime.fromisoformat(r["timestamp"]) for r in recs]
    assert ts[0] >= T0 and ts[-1] <= end
    assert ts == sorted(ts)  # monotonic, evenly spread


def test_gen_lines_deterministic(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    _gen_lines(1000, a, T0, seed=7)
    _gen_lines(1000, b, T0, seed=7)
    assert a.read_text() == b.read_text()


def test_gen_lines_has_error_clusters(tmp_path):
    # ~3% errors -> a 5000-line run has error lines for the clusterer to group
    p = tmp_path / "logs.jsonl"
    _gen_lines(5000, p, T0, seed=0)
    errs = [json.loads(x) for x in p.read_text().splitlines() if json.loads(x)["level"] == "error"]
    assert len(errs) > 0


def test_fmt_table_renders_rows():
    rows = [{"n": 10000, "ingest_wall_s": 4.4, "ingest_lines_per_s": 2254,
             "ingest_queries": 24, "explain_wall_s": 0.23, "explain_queries": 7, "peak_rss_mb": 110.9}]
    out = _fmt_table(rows)
    assert "lines" in out and "10,000" in out  # thousands-separated int
