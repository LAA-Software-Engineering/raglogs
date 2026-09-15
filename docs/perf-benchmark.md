# Performance benchmark: ingest → explain (#85)

Until now raglogs had **no committed benchmark and no stated performance target** — #85's
opening question, *"what is the largest window raglogs can explain?"*, had no answer. This is
the answer's first instalment: a reproducible harness (`scripts/bench_pipeline.py`, `make
bench`) that turns "I think this is slow" into "this operation costs X at N lines."

**This is measurement only — no optimization.** The point is to establish the curve *before*
touching pool sizing, bulk inserts, or partitioning, so each of those fixes can be judged
against a committed baseline instead of a hunch.

## What it measures

For each size N on the curve, in its own subprocess against a freshly-truncated table:

1. generate N synthetic JSONL lines (10 services, ~90% info / 7% warn / 3% error, templated so
   the clusterer builds real groups), spread across a 1 h window;
2. **ingest** them through the real `ingest_files` path (parse → normalize → fingerprint →
   bulk persist);
3. **explain** the whole window via `explain_window` (`no_llm`).

Recorded per phase: wall time, SQL statements issued (a `before_cursor_execute` counter),
ingest throughput (lines/s), and peak process RSS.

```bash
DB_URL=postgresql+psycopg://postgres:postgres@localhost:5433/raglogs make bench
# or a custom curve:
SIZES=10000,50000,250000 make bench
```

## Baseline curve

Reference hardware: **Intel i7-12650H (16 threads), 23 GiB RAM, WSL2**; PostgreSQL 16.15
(`pgvector`) in Docker on `localhost:5433`. `embeddings_provider=disabled`, `no_llm`. Taken
2026-09-15 with `scripts/bench_pipeline.py --sizes 10000,100000,500000,1000000`.

| lines | ingest s | lines/s | ingest q | explain s | explain q | peak MB |
|---|---|---|---|---|---|---|
| 10,000 | 4.31 | 2,321 | 24 | 0.25 | 7 | 109 |
| 100,000 | 42.96 | 2,328 | 204 | 1.99 | 7 | 188 |
| 500,000 | 215.03 | 2,325 | 1,004 | 10.70 | 7 | 568 |
| 1,000,000 | 422.95 | 2,364 | 2,004 | 22.02 | 7 | 1,042 |

What the curve says:

- **Ingest is the bottleneck, and it is the real limit** — a flat **~2,320 lines/s**, so a
  1M-line window takes **~7 minutes** to ingest. Ingest query count scales as expected for the
  existing `pg_insert` batching (2 statements per 500-line batch ≈ 2,004 for 1M), so the cost is
  **not** the database round-trips — it is per-line Python work (parse → normalize → fingerprint).
  That is where the next optimization pass should look, not at the DB.
- **Explain scales cleanly and is already efficient** — **22 s for 1M lines** and a *constant*
  **7 SQL statements** regardless of N. The per-row `ClusterMember` insert #85 worried about does
  not show up here: cluster persistence is already bulk (constant query count), so that item is
  effectively done. Explain wall time is the clusterer reading all N rows in the window — linear,
  ~22 µs/line.
- **Memory** grows ~linearly to **~1 GB peak at 1M lines** (generation + ingest buffers).

## Performance target

#85 asks for an explicit envelope. From the curve, the product-facing latency — how long a user
waits for an explanation — is the **explain** phase (ingest is continuous/background):

> **A 1M-line incident window should explain in under 10 s on the reference hardware.**
> Today it is **22 s** (2.2× over) — the gap this issue's clustering-path optimizations must close.

Secondary, operational: **ingest throughput ≥ 10k lines/s** (today ~2.3k, ~4× short) so a 1M-line
backlog ingests in ~1–2 min rather than 7. Both targets are set *after* the first measurement, not
before, so we are fixing a measured limit rather than speculative scale debt. This benchmark is now
the regression guard — a change that moves any column shows up in `make bench`.

## Reading the result / next steps (not in this PR)

The curve **re-orders #85's checklist by evidence.** The row-at-a-time `ClusterMember` insert the
issue flagged is already bulk (explain issues a constant 7 statements), so that item is done; the
real target is **ingest per-line CPU**, which the issue did not call out:

- **Profile and speed up ingest** (parse → normalize → fingerprint) — the flat 2,320 lines/s is
  Python-bound, not DB-bound, so bigger batches won't help; the win is in the per-line hot path.
- **Explain latency**: at 22 s for 1M lines the clusterer's full-window scan is the cost; worth a
  look only once ingest is addressed, since explain already meets a constant, small query budget.
- Lower-confidence / defer until the curve justifies: connection-pool sizing (a *concurrency*
  concern the single-stream benchmark does not exercise — needs the API load test), native time
  partitioning on `log_entries` (+ retention via partition drop), and the API load test with auth
  / rate-limit / LLM fallback.

Each optimization lands with a before/after `make bench` delta — the same evidence discipline the
RCA work used.
