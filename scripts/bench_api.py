#!/usr/bin/env python3
"""API concurrency / connection-pool load test for raglogs (#85).

``scripts/bench_pipeline.py`` measures the *single-stream* cost of one explain. It says
nothing about what happens when N users hit the API at once — which is the question
behind #85's "pool sizing" and "no load test" items. This harness answers it: it drives
the real ASGI app (``src.api.app:app``) over HTTP (in-process, ``httpx`` + ``ASGITransport``)
at rising concurrency and reports throughput and latency percentiles at each level, so the
connection-pool saturation knee is a measured curve instead of a guess.

**Measurement only — no optimization.** It establishes the concurrency baseline the pool /
worker sizing work in #85 will be judged against.

What it does, per run:
  1. seed ``--lines`` synthetic JSONL lines through the real ``ingest_files`` path into a
     fixed 1 h window (reusing ``bench_pipeline``'s generator);
  2. for each concurrency level C in ``--concurrency``, fire ``--requests`` POSTs to
     ``/v1/query/explain`` (``no_llm``, ``force_refresh`` so every call runs the full
     pipeline and holds a DB connection — not the explanation cache), at most C in flight;
  3. record wall time, throughput (req/s), latency p50/p95/p99/max, and error / HTTP-429
     counts.

Rate limiting is disabled (``RATELIMIT_ENABLED=false``) so the shared "anonymous" bucket is
not the bottleneck; the LLM is left ``disabled`` (Noop, no network). The anyio threadpool
limiter is raised above the max concurrency tested so the **DB connection pool**
(``DB_POOL_SIZE`` + ``DB_MAX_OVERFLOW``, default 20+20) is the isolated variable.

    DB_URL=postgresql+psycopg://postgres:postgres@localhost:5433/raglogs \
        python scripts/bench_api.py --lines 20000 --concurrency 1,2,4,8,16,32,64 --requests 200

    # provoke pool starvation: a small pool, concurrency past it
    ... python scripts/bench_api.py --pool-size 5 --max-overflow 0 --concurrency 1,4,8,16
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import logging  # noqa: E402
import warnings  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.bench_pipeline import WINDOW_SECONDS, _gen_lines, _truncate  # noqa: E402


def _configure_env_and_logging() -> None:
    """Set the env the harness needs (before any settings/engine/app import) and quiet
    the per-request structlog output. Done in main(), not at import, so importing this
    module for its pure helpers has no global side effects."""
    os.environ.setdefault("RATELIMIT_ENABLED", "false")
    os.environ.setdefault("LLM_PROVIDER", "disabled")
    warnings.filterwarnings("ignore")
    logging.getLogger().setLevel(logging.WARNING)


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _seed(lines: int, scope: str, t0: datetime) -> str:
    """Ingest ``lines`` synthetic rows; return the ingestion_job_id."""
    from src.core.ingestion.service import ingest_files
    from src.db.session import get_db, get_engine

    _truncate(get_engine())
    tmp = Path(tempfile.gettempdir()) / f"benchapi_{lines}_{os.getpid()}.jsonl"
    _gen_lines(lines, tmp, t0, seed=0)
    try:
        with get_db() as db:
            job, stats = ingest_files(db=db, paths=[str(tmp)], scope=scope)
            job_id = str(job.id)  # read before the session closes (avoid DetachedInstanceError)
            parsed = stats.parsed_count
        print(f"[seed] ingested {parsed} lines (scope={scope})", file=sys.stderr)
        return job_id
    finally:
        tmp.unlink(missing_ok=True)


async def _warm_cache(app, body: dict) -> None:
    """Populate the explanation cache for this window so a force_refresh=false sweep
    measures the cache read path, not a first-time full-pipeline miss."""
    import httpx

    warm_body = {**body, "force_refresh": True}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://bench", timeout=60.0) as client:
        await client.post("/v1/query/explain", json=warm_body)


async def _run_level(app, body: dict, concurrency: int, requests: int) -> dict:
    import httpx

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors = 0
    throttled = 0

    async with httpx.AsyncClient(transport=transport, base_url="http://bench", timeout=60.0) as client:
        async def one() -> None:
            nonlocal errors, throttled
            async with sem:
                t = time.perf_counter()
                try:
                    resp = await client.post("/v1/query/explain", json=body)
                    dt = time.perf_counter() - t
                    latencies.append(dt)
                    if resp.status_code == 429:
                        throttled += 1
                    elif resp.status_code >= 400:
                        errors += 1
                except Exception:
                    latencies.append(time.perf_counter() - t)
                    errors += 1

        t0 = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(requests)))
        wall = time.perf_counter() - t0

    latencies.sort()
    return {
        "concurrency": concurrency,
        "requests": requests,
        "wall_s": round(wall, 3),
        "throughput_rps": round(requests / wall, 1) if wall else None,
        "p50_ms": round(_percentile(latencies, 0.50) * 1000, 1),
        "p95_ms": round(_percentile(latencies, 0.95) * 1000, 1),
        "p99_ms": round(_percentile(latencies, 0.99) * 1000, 1),
        "max_ms": round(latencies[-1] * 1000, 1) if latencies else None,
        "errors": errors,
        "http_429": throttled,
    }


def _raise_thread_limiter(target: int) -> None:
    """Raise anyio's default thread limiter so sync endpoints are not throttled below the
    DB pool — the pool is what we want to characterize, not the threadpool."""
    try:
        import anyio.to_thread

        limiter = anyio.to_thread.current_default_thread_limiter()
        if limiter.total_tokens < target:
            limiter.total_tokens = target
    except Exception:
        pass


async def _main_async(args) -> int:
    from src.api.app import app

    # The app configures structlog at INFO and prints every request to stdout; raise the
    # filter so the benchmark output is just the tables (measurement, not request logs).
    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    window_end = t0 + timedelta(seconds=WINDOW_SECONDS)
    scope = f"benchapi:{args.lines}"
    job_id = _seed(args.lines, scope, t0)

    body = {
        "from_time": t0.isoformat(),
        "to_time": window_end.isoformat(),
        "no_llm": True,
        "force_refresh": True,
        "scope": scope,
        "ingestion_job_id": job_id,
    }

    levels = [int(c) for c in args.concurrency.split(",") if c.strip()]
    _raise_thread_limiter(max(levels) + 8)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    for mode in modes:
        force_refresh = mode == "pipeline"
        mode_body = {**body, "force_refresh": force_refresh}
        if not force_refresh:
            await _warm_cache(app, body)  # populate the cache first
        # warm one request (build engine, JIT caches, plan the queries)
        await _run_level(app, mode_body, concurrency=1, requests=2)

        rows = []
        for c in levels:
            print(f"[bench-api:{mode}] concurrency={c} …", file=sys.stderr, flush=True)
            rows.append(await _run_level(app, mode_body, concurrency=c, requests=args.requests))
        _print_table(rows, args, mode)
    return 0


def _print_table(rows: list[dict], args, mode: str) -> None:
    head = ("conc", "req", "wall s", "req/s", "p50 ms", "p95 ms", "p99 ms", "max ms", "err", "429")
    keys = ("concurrency", "requests", "wall_s", "throughput_rps",
            "p50_ms", "p95_ms", "p99_ms", "max_ms", "errors", "http_429")
    widths = [max(len(h), 7) for h in head]
    label = {"pipeline": "explain, force_refresh (full pipeline, CPU-bound)",
             "cached": "explain, cache hit (DB read, I/O-bound)"}.get(mode, mode)
    print(f"\n=== raglogs API concurrency benchmark (#85) — {label} ===")
    pool = f"{os.getenv('DB_POOL_SIZE', '20')}+{os.getenv('DB_MAX_OVERFLOW', '20')}"
    print(f"lines: {args.lines:,}  |  pool: {pool}  |  ratelimit: {os.getenv('RATELIMIT_ENABLED')}  "
          f"|  DB: {os.getenv('DB_URL', '').rsplit('@', 1)[-1]}\n")
    print("  ".join(h.rjust(w) for h, w in zip(head, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(f"{r.get(k, ''):>{w}}" for k, w in zip(keys, widths)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lines", type=int, default=20000, help="synthetic lines to seed")
    ap.add_argument("--concurrency", default="1,2,4,8,16,32,64",
                    help="comma-separated in-flight concurrency levels")
    ap.add_argument("--requests", type=int, default=200, help="requests per concurrency level")
    ap.add_argument("--modes", default="pipeline,cached",
                    help="comma-separated: 'pipeline' (force_refresh, CPU-bound) and/or "
                         "'cached' (cache hit, I/O-bound)")
    ap.add_argument("--pool-size", type=int, help="override DB_POOL_SIZE for this run")
    ap.add_argument("--max-overflow", type=int, help="override DB_MAX_OVERFLOW for this run")
    args = ap.parse_args()

    _configure_env_and_logging()

    if args.pool_size is not None:
        os.environ["DB_POOL_SIZE"] = str(args.pool_size)
    if args.max_overflow is not None:
        os.environ["DB_MAX_OVERFLOW"] = str(args.max_overflow)

    if not os.getenv("DB_URL"):
        print("DB_URL must be set (a Postgres/pgvector instance)", file=sys.stderr)
        return 2

    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
