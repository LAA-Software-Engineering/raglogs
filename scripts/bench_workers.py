#!/usr/bin/env python3
"""Out-of-process multi-worker scaling test for the raglogs API (#85, option 2).

The in-process API benchmark (`scripts/bench_api.py`) showed the explain path is
GIL/CPU-bound: a single process peaks near concurrency 2 and the connection pool is
never the bottleneck. The remedy the load test pointed to is **process-level
parallelism** — run several uvicorn workers. This harness measures whether that
actually pays off, and does it the way the #191 review asked for: a **real uvicorn
server in a separate OS process** driven over a **real socket** (no shared GIL with the
load generator), which the in-process harness cannot exercise.

Per worker-count W in the sweep it:
  1. starts `uvicorn src.api.app:app --workers W` as a subprocess (rate-limit off, LLM
     disabled, a modest per-worker DB pool so W workers stay under Postgres
     max_connections);
  2. waits for `GET /health`;
  3. drives `POST /v1/query/explain` (full pipeline: `no_llm`, `force_refresh`) at a
     fixed concurrency over real HTTP, recording throughput and latency percentiles;
  4. tears the server down.

The dataset is seeded once, in-process, before the sweep (reusing bench_api/bench_pipeline).

    DB_URL=postgresql+psycopg://postgres:postgres@localhost:5433/raglogs \
        python scripts/bench_workers.py --lines 10000 --workers 1,2,4,8 --concurrency 16 --requests 240
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.bench_api import _percentile, _seed  # noqa: E402
from scripts.bench_pipeline import WINDOW_SECONDS  # noqa: E402


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(port: int, proc: subprocess.Popen, timeout: float = 40.0) -> bool:
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:  # server died during startup
            return False
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
            if r.status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _start_server(workers: int, port: int, pool: int, overflow: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        RATELIMIT_ENABLED="false",
        LLM_PROVIDER="disabled",
        DB_POOL_SIZE=str(pool),
        DB_MAX_OVERFLOW=str(overflow),
    )
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.api.app:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--workers", str(workers), "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


async def _drive(base_url: str, body: dict, concurrency: int, requests: int) -> dict:
    import httpx

    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors = 0
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:
        async def one() -> None:
            nonlocal errors
            async with sem:
                t = time.perf_counter()
                try:
                    r = await client.post("/v1/query/explain", json=body)
                    latencies.append(time.perf_counter() - t)
                    if r.status_code >= 400:
                        errors += 1
                except Exception:
                    latencies.append(time.perf_counter() - t)
                    errors += 1

        # warm the workers, then measure
        await asyncio.gather(*(one() for _ in range(min(concurrency, requests))))
        latencies.clear()
        errors = 0
        t0 = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(requests)))
        wall = time.perf_counter() - t0

    latencies.sort()
    return {
        "wall_s": round(wall, 3),
        "throughput_rps": round(requests / wall, 1) if wall else None,
        "p50_ms": round(_percentile(latencies, 0.50) * 1000, 1),
        "p95_ms": round(_percentile(latencies, 0.95) * 1000, 1),
        "p99_ms": round(_percentile(latencies, 0.99) * 1000, 1),
        "errors": errors,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lines", type=int, default=10000)
    ap.add_argument("--workers", default="1,2,4,8", help="comma-separated worker counts")
    ap.add_argument("--concurrency", type=int, default=16, help="fixed in-flight client requests")
    ap.add_argument("--concurrency-per-worker", type=int, default=None,
                    help="if set, concurrency = this × workers for each point (each worker "
                         "peaks near ~2 concurrent, so this measures the per-worker-optimal "
                         "scaling ceiling instead of a fixed total load)")
    ap.add_argument("--requests", type=int, default=240, help="measured requests per worker count")
    ap.add_argument("--pool-size", type=int, default=5, help="DB_POOL_SIZE per worker")
    ap.add_argument("--max-overflow", type=int, default=5, help="DB_MAX_OVERFLOW per worker")
    ap.add_argument("--modes", default="pipeline",
                    help="comma-separated: 'pipeline' (force_refresh, full pipeline + DB) and/or "
                         "'cached' (cache hit, one cheap SELECT) — the contrast isolates whether "
                         "multi-worker scaling is bounded by shared Postgres CPU or the workers")
    args = ap.parse_args()

    if not os.getenv("DB_URL"):
        print("DB_URL must be set (a Postgres/pgvector instance)", file=sys.stderr)
        return 2

    # keep W workers under Postgres max_connections
    worker_counts = [int(w) for w in args.workers.split(",") if w.strip()]
    per_worker = args.pool_size + args.max_overflow
    max_conns = max(worker_counts) * per_worker
    print(f"[bench-workers] peak DB connections ≈ {max_conns} "
          f"({max(worker_counts)} workers × {per_worker})", file=sys.stderr)

    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    window_end = t0 + timedelta(seconds=WINDOW_SECONDS)
    scope = f"benchworkers:{args.lines}"
    job_id = _seed(args.lines, scope, t0)
    base_body = {
        "from_time": t0.isoformat(),
        "to_time": window_end.isoformat(),
        "no_llm": True,
        "scope": scope,
        "ingestion_job_id": job_id,
    }
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    if "cached" in modes:  # populate the explanation cache once (persists in the DB)
        port = _find_free_port()
        proc = _start_server(1, port, args.pool_size, args.max_overflow)
        try:
            if _wait_healthy(port, proc):
                asyncio.run(_drive(f"http://127.0.0.1:{port}", {**base_body, "force_refresh": True}, 1, 1))
        finally:
            _stop_server(proc)

    for mode in modes:
        body = {**base_body, "force_refresh": mode == "pipeline"}
        rows = []
        for w in worker_counts:
            port = _find_free_port()
            proc = _start_server(w, port, args.pool_size, args.max_overflow)
            try:
                if not _wait_healthy(port, proc):
                    print(f"[bench-workers] {mode} {w}w failed to start", file=sys.stderr)
                    _stop_server(proc)
                    continue
                conc = args.concurrency_per_worker * w if args.concurrency_per_worker else args.concurrency
                print(f"[bench-workers:{mode}] workers={w} concurrency={conc} …", file=sys.stderr, flush=True)
                result = asyncio.run(_drive(f"http://127.0.0.1:{port}", body, conc, args.requests))
                result["workers"] = w
                result["concurrency"] = conc
                rows.append(result)
            finally:
                _stop_server(proc)
        _print_table(rows, args, mode)
    return 0


def _print_table(rows: list[dict], args, mode: str) -> None:
    head = ("workers", "conc", "req/s", "vs 1w", "p50 ms", "p95 ms", "p99 ms", "err")
    widths = [max(len(h), 7) for h in head]
    conc_mode = (f"{args.concurrency_per_worker}×workers" if args.concurrency_per_worker
                 else str(args.concurrency))
    label = {"pipeline": "full pipeline (force_refresh, DB-heavy)",
             "cached": "cache hit (one cheap SELECT)"}.get(mode, mode)
    print(f"\n=== raglogs API multi-worker scaling (#85) — {label} ===")
    print(f"lines: {args.lines:,}  |  concurrency: {conc_mode}  |  "
          f"pool/worker: {args.pool_size}+{args.max_overflow}  |  "
          f"DB: {os.getenv('DB_URL', '').rsplit('@', 1)[-1]}\n")
    print("  ".join(h.rjust(w) for h, w in zip(head, widths)))
    print("  ".join("-" * w for w in widths))
    base = rows[0]["throughput_rps"] if rows and rows[0].get("throughput_rps") else None
    for r in rows:
        speedup = f"{r['throughput_rps'] / base:.2f}x" if base else "-"
        cells = (r["workers"], r.get("concurrency", args.concurrency), r["throughput_rps"], speedup,
                 r["p50_ms"], r["p95_ms"], r["p99_ms"], r["errors"])
        print("  ".join(f"{c:>{w}}" for c, w in zip(cells, widths)))


if __name__ == "__main__":
    raise SystemExit(main())
