"""Benchmark harness: synthetic-load generation, query counting, timing.

The generator and counter are pure/engine-agnostic so they unit-test without
Postgres; :func:`run_benchmark` wires them to the real ingest + explain path.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Rough target so a run has something to pass/fail against; refine empirically
# and record the real limit in the README (issue #85).
TARGET_EXPLAIN_SECONDS = 10.0
TARGET_LINES = 1_000_000


def generate_jsonl_lines(n_lines: int, base: datetime | None = None) -> list[dict]:
    """Generate N incident-shaped log records spread over a window.

    A deploy trigger near the start, then a rising share of error lines from one
    service (so clustering + trigger detection have real work), plus benign
    traffic. Deterministic given ``base`` so benchmarks are comparable.
    """
    if n_lines <= 0:
        return []
    base = base or datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    span = timedelta(minutes=30)
    step = span / max(n_lines, 1)

    lines: list[dict] = []
    for i in range(n_lines):
        ts = base + step * i
        if i == 0:
            lines.append(
                {
                    "timestamp": ts.isoformat(),
                    "level": "info",
                    "service": "deployer",
                    "message": "Deploy completed for billing-worker v2.4.1",
                }
            )
        elif i % 3 == 0:
            lines.append(
                {
                    "timestamp": ts.isoformat(),
                    "level": "error",
                    "service": "billing-worker",
                    "message": "Stripe signature verification failed for endpoint /webhooks/stripe",
                }
            )
        else:
            lines.append(
                {
                    "timestamp": ts.isoformat(),
                    "level": "info",
                    "service": "api",
                    "message": f"POST /api/checkout 200 OK latency={100 + (i % 50)}ms",
                }
            )
    return lines


def write_jsonl(lines: list[dict], path: Path) -> Path:
    path = Path(path)
    with path.open("w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


class QueryCounter:
    """Count SQL statements executed on an engine within a ``with`` block."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.count = 0

    def _on_execute(self, *args) -> None:
        self.count += 1

    def __enter__(self) -> "QueryCounter":
        from sqlalchemy import event

        event.listen(self.engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc) -> None:
        from sqlalchemy import event

        event.remove(self.engine, "before_cursor_execute", self._on_execute)


@dataclass
class BenchResult:
    n_lines: int
    ingested: int
    total_logs: int
    ingest_seconds: float
    ingest_queries: int
    explain_seconds: float
    explain_queries: int
    target_explain_seconds: float = TARGET_EXPLAIN_SECONDS

    @property
    def meets_target(self) -> bool:
        return self.explain_seconds <= self.target_explain_seconds

    def to_dict(self) -> dict:
        d = asdict(self)
        d["meets_target"] = self.meets_target
        d["generated_at"] = datetime.now(tz=timezone.utc).isoformat()
        return d


def format_report(result: BenchResult) -> str:
    lines = [
        f"raglogs benchmark — {result.n_lines:,} lines",
        "-" * 44,
        f"ingest:   {result.ingest_seconds:7.3f}s  {result.ingest_queries:>6} queries  "
        f"({result.ingested:,} rows)",
        f"explain:  {result.explain_seconds:7.3f}s  {result.explain_queries:>6} queries  "
        f"({result.total_logs:,} logs in window)",
        "",
        f"target:   explain <= {result.target_explain_seconds:.0f}s  ->  "
        f"{'PASS' if result.meets_target else 'FAIL'}",
    ]
    return "\n".join(lines)


def run_benchmark(db, engine, n_lines: int, tmp_dir: Path, scope: str | None = None) -> BenchResult:
    """Ingest N synthetic lines and explain the window, timing each phase."""
    from src.core.explain.summarizer import explain_window
    from src.core.ingestion.service import ingest_files

    scope = scope or f"bench:{uuid.uuid4().hex[:8]}"
    lines = generate_jsonl_lines(n_lines)
    log_file = write_jsonl(lines, Path(tmp_dir) / "bench.jsonl")

    with QueryCounter(engine) as qc:
        t0 = time.perf_counter()
        job, stats = ingest_files(db=db, paths=[str(log_file)], scope=scope)
        ingest_seconds = time.perf_counter() - t0
        ingest_queries = qc.count

    window_start = datetime.fromisoformat(lines[0]["timestamp"]) - timedelta(minutes=1)
    window_end = datetime.fromisoformat(lines[-1]["timestamp"]) + timedelta(minutes=1)

    with QueryCounter(engine) as qc:
        t0 = time.perf_counter()
        result = explain_window(
            db=db,
            window_start=window_start,
            window_end=window_end,
            ingestion_job_id=job.id,
            scope=scope,
            no_llm=True,
        )
        explain_seconds = time.perf_counter() - t0
        explain_queries = qc.count

    return BenchResult(
        n_lines=n_lines,
        ingested=stats.parsed_count,
        total_logs=result.total_logs,
        ingest_seconds=ingest_seconds,
        ingest_queries=ingest_queries,
        explain_seconds=explain_seconds,
        explain_queries=explain_queries,
    )
