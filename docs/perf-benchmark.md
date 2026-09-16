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

## Optimization log

### 1. ISO-8601 timestamp fast path (2026-09-15)

The ingest profile (`cProfile` over 100k lines) put `dateutil.parser.parse` at ~12% of ingest
wall — it was parsing *every* line's timestamp with a generic, pure-Python parser, even though
log timestamps are overwhelmingly ISO 8601. `parse_timestamp_field` now tries the stdlib C parser
(`datetime.fromisoformat`, via the shared `rewrite_iso_z`) first and only falls back to `dateutil`
for anything it rejects — behaviour-preserving for the ISO strings this codebase realistically
sees (36 timestamp / parsing / normalization tests unchanged). One known divergence (#176 review):
a UTC offset with a *seconds* component (e.g. `+02:00:30`) — `fromisoformat` keeps full precision
where the old `dateutil`→regex path truncated it to `+02:00`; more correct, and no real log source
emits sub-minute offsets.

Component microbenchmark (200k identical ISO timestamps, same machine):

| parser | per timestamp | 200k |
|---|---|---|
| `dateutil.parse` | 108.1 µs | 21.6 s |
| `fromisoformat` | 0.4 µs | 0.075 s |

**286× faster** on the parse itself; since timestamp parsing was ~12% of ingest wall, the
end-to-end effect is proportionate: **1M-line ingest 2,364 → 2,661 lines/s (423 s → 376 s, ≈ +12%)**
in a single before/after run (small sizes are within run-to-run noise). **Explain is unchanged** (it
does not parse timestamps).

### 2. Bulk INSERT via executemany / insertmanyvalues (2026-09-15)

A coarse phase split (undistorted timers, not `cProfile`) then showed **persist was ~85% of ingest
wall** (`_process_line` only ~15%), and within persist, `db.execute` ~67% / statement build ~16%.
The cost was the shape of the insert: `pg_insert(LogEntry).values([500 dicts])` builds one giant
multi-`VALUES` statement — 500 × 20-column bind params to *compile* in SQLAlchemy **and** re-parse
in psycopg (`_split_query`) every batch. Switched to a **parameterless** statement executed with the
value list (`db.execute(stmt, [values, …])`), so SQLAlchemy's insertmanyvalues compiles once and
reuses it. `RETURNING id` + `_entries_inserted`'s id-set correlation keep the dedup accounting
identical (verified: a re-ingest of the same file dedups 5,000/5,000; the executemany form was
measured at 3.6× the old form on a 20k isolated insert).

| lines | ingest lines/s (before → after) | 1M ingest wall |
|---|---|---|
| 100,000 | 2,328 → **5,882** | — |
| 1,000,000 | 2,661 → **5,766** | **376 s → 173 s** |

**~2.2× faster ingest** (≈ **2.5×** over the pre-#85 baseline once the timestamp fast path is
included). Explain and peak RSS unchanged. Eval delta: **none** — identical rows/dedup, output
unchanged.

The remaining ingest cost is now split between the per-line work (parse/normalize/fingerprint) and
the DB round-trip itself; further wins (e.g. `COPY`, larger batches, partitioning) get evaluated
against this new curve, cheapest first — but ingest is no longer the glaring bottleneck it was.

### 3. Bound the trigger scan at the primary error onset (2026-09-15)

An explain profile (`cProfile`, 500k) put `find_trigger_candidates` at ~53% of explain, and a
direct timing confirmed it: the trigger query — a regex `~*` over the trigger patterns with no
usable index — scanned the **whole** incident window (`[window_start − lookback, window_end]`),
~5 s for 1M rows even with 0 matches (the regex evaluates on every in-range row).

**Which patterns can be bounded (the #189-review correction).** A *causal precursor* — deploy,
config change, migration, release, rollout (`CAUSAL_TRIGGER_PATTERNS`) — that fired *after* the
errors began did not cause them, so its search can be bounded at the primary cluster's onset
(`first_seen`, already computed). But `TRIGGER_PATTERNS` also holds *reactive* patterns —
circuit-breaker trips, pod evictions, queue saturation, restarts, token expiry
(`REACTIVE_TRIGGER_PATTERNS`) — that commonly fire *after* onset; bounding those would drop a real
trigger and flip the `bool(trigger_candidates)` confidence gate (medium → low). So only the causal
patterns are bounded at onset; the reactive ones are always searched through `window_end`.

Direct trigger-query timing on 1M rows (2 warm runs):

| trigger query | wall |
|---|---|
| unbounded (whole window, all patterns) | 5.23 s |
| causal-bounded at onset, reactive unbounded | **2.46 s** |

So the causal bound roughly halves the trigger scan (the reactive full-window scan is now the
floor). End-to-end explain improves by ~the same ~2.7 s at 1M; the benchmark's explain wall is
noisy (±several seconds, run to run) and dominated by the clustering full-window scan (~10–15 s at
1M, deliberately left for a later cycle), so the trigger-query delta above is the reliable number.

**Eval delta: none on the available corpora; behaviour change precisely scoped.** The only
observable change is that a *causal* trigger occurring **after** onset is no longer returned
(correct — it cannot be the cause); *reactive* triggers are unaffected, so the confidence gate is
preserved for them (reproduced: a case whose only trigger is a post-onset circuit-breaker trip
keeps its label with the bound — integration test `test_search_end_does_not_bound_reactive_triggers`).
Measured on the OTel frozen corpus: **0 / 41,638** log lines match any trigger pattern, so
`find_trigger_candidates` returns empty with and without the change.
