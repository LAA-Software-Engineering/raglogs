#!/usr/bin/env python3
"""Reproducible performance benchmark for the ingest → explain pipeline (#85).

Answers the question #85 says nobody can today: *what does it cost to explain a window
of N log lines?* It generates N synthetic lines, ingests them through the real
``ingest_files`` path (parse → normalize → fingerprint → bulk persist), then runs
``explain_window`` over the whole window, measuring for each phase:

  * wall time (``perf_counter``)
  * SQL statements issued (a ``before_cursor_execute`` counter on the engine)
  * ingest throughput (lines / sec)
  * peak process RSS (``ru_maxrss``)

**This PR is measurement only — no optimization.** It turns "I think this is slow" into
"this operation costs X at N rows", so the fixes in #85 (bulk ClusterMember insert, pool
sizing, partitioning, …) can be judged against a committed curve instead of a hunch.

Each size runs in its own **subprocess** (driver mode) so peak RSS and DB state are clean
per point; the worker truncates the log tables first, so a point measures N lines on an
otherwise-empty table. Needs Postgres (``DB_URL``); nothing is left in the DB you point it at
beyond the last run's rows.

    DB_URL=postgresql+psycopg://postgres:postgres@localhost:5433/raglogs \
        python scripts/bench_pipeline.py --sizes 10000,100000,500000,1000000

    # one point (worker mode; prints a JSON metrics line):
    python scripts/bench_pipeline.py --single 100000
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

WINDOW_SECONDS = 3600  # spread each run's lines across a 1h incident window
_SERVICES = ["api", "web", "auth", "payments", "orders", "cart", "search", "email", "worker", "db"]
# Templates normalise to a handful of fingerprints, so clustering has real groups to build.
_INFO = [
    "GET /api/users/12345 200 latency=42ms",
    "POST /api/checkout 200 OK latency=115ms",
    "cache hit key=session:abcdef ttl=300",
    "processed job id=98765 in 12ms",
    "GET /api/products/555 200 latency=8ms",
]
_WARN = ["slow query took 1200ms on table orders", "retry 2/3 for upstream call to payments"]
_ERROR = [
    "ERROR payment gateway timeout for order 44219",
    "ERROR unhandled exception NullPointerException at line 84",
    "ERROR connection refused to db host 10.0.0.5:5432",
]


def _gen_lines(n: int, path: Path, t0: datetime, seed: int = 0) -> None:
    """Write ``n`` JSONL log lines spread evenly across the window (90% info / 7% warn /
    3% error), deterministically."""
    import random

    rng = random.Random(seed)
    step = timedelta(seconds=WINDOW_SECONDS / max(n, 1))
    with path.open("w") as f:
        for i in range(n):
            r = rng.random()
            if r < 0.90:
                level, msg = "info", rng.choice(_INFO)
            elif r < 0.97:
                level, msg = "warn", rng.choice(_WARN)
            else:
                level, msg = "error", rng.choice(_ERROR)
            rec = {
                "timestamp": (t0 + step * i).isoformat(),
                "level": level,
                "service": _SERVICES[i % len(_SERVICES)],
                "message": msg,
            }
            f.write(json.dumps(rec))
            f.write("\n")


class _QueryCounter:
    """Counts SQL statements on an engine between ``reset`` calls."""

    def __init__(self, engine):
        from sqlalchemy import event

        self.count = 0
        event.listen(engine, "before_cursor_execute", self._on)

    def _on(self, *_args, **_kwargs):
        self.count += 1

    def reset(self) -> int:
        n, self.count = self.count, 0
        return n


def _truncate(engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE log_entries, clusters, cluster_members, cluster_runs, ingestion_jobs "
            "RESTART IDENTITY CASCADE"
        ))


def _peak_rss_mb() -> float:
    # ru_maxrss is KB on Linux, bytes on macOS; assume Linux (the reference platform).
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def run_single(n: int, seed: int = 0) -> dict:
    """Measure one point: generate + ingest + explain N lines on a truncated table."""
    from src.core.explain.summarizer import explain_window
    from src.core.ingestion.service import ingest_files
    from src.db.session import get_db, get_engine

    engine = get_engine()
    counter = _QueryCounter(engine)
    _truncate(engine)

    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    window_end = t0 + timedelta(seconds=WINDOW_SECONDS)
    scope = f"bench:{n}"

    tmp = Path(tempfile.gettempdir()) / f"bench_{n}_{os.getpid()}.jsonl"
    _gen_lines(n, tmp, t0, seed=seed)
    try:
        with get_db() as db:
            counter.reset()
            t = time.perf_counter()
            job, stats = ingest_files(db=db, paths=[str(tmp)], scope=scope)
            ingest_wall = time.perf_counter() - t
            ingest_q = counter.reset()

            t = time.perf_counter()
            result = explain_window(
                db=db, window_start=t0, window_end=window_end,
                ingestion_job_id=job.id, scope=scope, no_llm=True,
            )
            explain_wall = time.perf_counter() - t
            explain_q = counter.reset()
    finally:
        tmp.unlink(missing_ok=True)

    return {
        "n": n,
        "parsed": stats.parsed_count,
        "ingest_wall_s": round(ingest_wall, 3),
        "ingest_lines_per_s": round(n / ingest_wall) if ingest_wall else None,
        "ingest_queries": ingest_q,
        "explain_wall_s": round(explain_wall, 3),
        "explain_queries": explain_q,
        "clusters": len(result.secondary_clusters) + (1 if result.primary_cluster else 0),
        "peak_rss_mb": round(_peak_rss_mb(), 1),
    }


def _fmt_table(rows: list[dict]) -> str:
    head = ("lines", "ingest s", "lines/s", "ingest q", "explain s", "explain q", "peak MB")
    keys = ("n", "ingest_wall_s", "ingest_lines_per_s", "ingest_queries",
            "explain_wall_s", "explain_queries", "peak_rss_mb")
    widths = [max(len(h), 9) for h in head]
    out = ["  ".join(h.rjust(w) for h, w in zip(head, widths))]
    out.append("  ".join("-" * w for w in widths))
    for r in rows:
        out.append("  ".join(f"{r.get(k, ''):>{w},}" if isinstance(r.get(k), int)
                             else f"{r.get(k, ''):>{w}}" for k, w in zip(keys, widths)))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sizes", default="10000,100000,500000,1000000",
                    help="comma-separated line counts for the curve (driver mode)")
    ap.add_argument("--single", type=int, help="worker mode: measure one size, print JSON")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if args.single is not None:
        print(json.dumps(run_single(args.single, seed=args.seed)))
        return 0

    if not os.getenv("DB_URL"):
        print("DB_URL must be set (a Postgres/pgvector instance)", file=sys.stderr)
        return 2

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    rows: list[dict] = []
    for n in sizes:
        print(f"[bench] {n:,} lines …", file=sys.stderr, flush=True)
        proc = subprocess.run(
            [sys.executable, __file__, "--single", str(n), "--seed", str(args.seed)],
            capture_output=True, text=True, env=os.environ,
        )
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
            return proc.returncode
        rows.append(json.loads(proc.stdout.strip().splitlines()[-1]))

    print("\n=== raglogs ingest→explain benchmark (#85) ===")
    print(f"window: {WINDOW_SECONDS}s  |  DB: {os.getenv('DB_URL', '').rsplit('@', 1)[-1]}\n")
    print(_fmt_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
