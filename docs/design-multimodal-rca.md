# Design: multi-modal root-cause ranking (#118, Phase C build)

**Status:** draft for review. Proposes how to turn the validated spike
(`docs/spike-multimodal-rca.md`: 48.9% top-1 RE3 leave-one-system-out, +20pp)
into real `src/core` work, **without breaking the logs-only path**.

## Principles (unchanged)

- **Logs stay the default and the fallback.** Traces/metrics are *additive*
  evidence; with neither, the pipeline behaves exactly as today (the `noop`
  discipline). No corpus, no regression.
- **Evidence-based, not a black box.** Candidate generation → per-service
  features → learned ranking → calibrated confidence. The ranker's inputs are
  the same human-readable features the explanation already shows.
- **Every step lands behind the eval harness** with a leave-one-service /
  -fault / -system-out delta. No random splits.

## Data model (new, additive)

Two new tables, each independent of `log_entries`, joined only by
`(scope, service, time window)` — no foreign key to logs:

- `trace_spans`: `id, scope, trace_id, span_id, parent_span_id, service,
  operation, start_time, duration_ms, status_code, attributes, ingestion_job_id`.
  Indexed on `(scope, service, start_time)` and `(scope, trace_id)`.
- `metric_samples`: `id, scope, service, metric, value, ts, attributes,
  ingestion_job_id`. Indexed on `(scope, service, metric, ts)`. (Long format;
  the RCAEval `{service}_{metric}` wide columns are melted on ingest.)
- **`attributes JSONB` (nullable) on both** — an escape hatch for real telemetry
  dimensions the first algorithm ignores but must not be walled out of: metrics
  carry `{method, route, status, region, …}`, spans carry span attributes. The
  RCAEval ingest leaves it null; adding it now avoids a schema-corner later
  (ChatGPT review point 5).

Added via a **new Alembic migration** (never edit an applied one). `pgvector`
untouched.

## Ingestion (new adapters, parallel to logs)

- `src/adapters/traces/` and `src/adapters/metrics/`: each yields a typed record
  (`ParsedSpan` / `ParsedMetricSample`), mirroring how log adapters yield
  `ParsedLogLine`. Batch-persist like the log ingest path.
- RCAEval converter (`src/eval/rcaeval.py`): extend to also convert
  `traces.parquet` / `metrics.parquet` into these records for eval cases.
  Corpus-specific quirks handled here, not in core: RE3 spans have **null
  `status_code`** (failure inferred from anomaly), sock-shop ships **no traces**
  (metrics only). The core must treat any modality as optionally-absent.

## Feature computation (`src/core/rca/features.py`)

For an incident window + a baseline window, per **candidate service** (union of
services seen in any modality), compute exactly the spike's features:

| feature | source | note |
|---|---|---|
| `log_err` | clusters | error-level lines for the service |
| `log_grp` | clusters | largest `(service, fingerprint)` error group |
| `log_stack` | logs | originating stack-trace lines (frame regex) |
| `tr_rate` | trace_spans | span-rate ratio incident/baseline |
| `tr_dur` | trace_spans | p95 duration ratio |
| `met_anom` | metric_samples | max per-metric change ratio |
| `has_logs` / `has_traces` / `has_metrics` | ingest | modality *presence* (0/1) |

**Presence flags are mandatory, not optional (ChatGPT review point 2).** A
feature value of `0.0` is ambiguous — `tr_rate = 0` can mean "no trace anomaly"
*or* "no traces at all" (sock-shop). Those are different facts; without an
explicit `has_traces` the model can learn *corpus identity* from the missingness
pattern (sock-shop ≡ no-traces) — a shortcut, not RCA. So each feature ships
with its presence flag, and **training uses modality dropout**: replicate rows
with subsets masked (`logs+traces+metrics`, `logs+traces`, `logs+metrics`,
`logs-only`, …) so the ranker learns to *degrade gracefully* when a modality is
absent rather than to recognise which system it's looking at. Baseline window
reuses `resolve_baseline_window` + the in-job baseline (#115).

## Ranker (`src/core/rca/ranker.py`)

- **Model lifecycle.** Train offline (`scripts/train_rca_ranker.py`) on labeled
  corpora → serialize a small model artifact (gradient-boosted trees; the spike
  used sklearn defaults) checked into `models/` (versioned). At inference: load
  once, score the candidate feature matrix, rank.
- **Serialization: never pickle.** A pickled sklearn model executes arbitrary
  code on load and is undiffable. Instead export the tree ensemble to a **plain
  JSON** artifact (thresholds/leaf values per tree) — a ~KB file that is
  reviewable in PRs and safe to load — and score it with a tiny pure-Python
  evaluator (no sklearn needed at inference). ONNX is an alternative but heavier.
- **Fallback keys on the *model artifact*, not on sklearn.** `scikit-learn` is
  already a hard dependency (clustering uses it), so absence-of-sklearn is not
  the trigger. When the **model artifact is absent** (or no non-log modality is
  present for the window), fall back to the current volume selector — same
  graceful-degradation contract as the LLM `noop` provider. The ranker is an
  *enhancement*, not a requirement.
- Output: an ordered list of `(service, P(root cause))`; the top is the primary,
  and `P` feeds confidence.

## Root cause is a modality-neutral candidate, not a log cluster (point 3)

The current model equates "root cause" with the primary *log cluster*. Multi-modal
RCA breaks that: a memory-leaking `payment-service` may have a huge metric anomaly
and be the trace-latency origin while emitting **no error cluster at all**, and
`checkout` may have 9,000 relayed 500s. Forcing the answer back through
`select_primary_cluster` would make the new architecture pretend to be log-only.

So the ranker's unit is a **`RootCauseCandidate`**, not a cluster:

```
RootCauseCandidate:
    service
    score                 # ranker output
    log_evidence:    list  # clusters (optional)
    trace_evidence:  list  # span-latency / rate anomalies (optional)
    metric_evidence: list  # metric change-points (optional)
```

A log cluster becomes *one evidence type*, not the identity of the root cause.
`assemble_evidence` returns the top candidate; the explanation renders whatever
evidence it has (logs and/or traces and/or metrics), honestly naming which
signal implicated the service — e.g. "memory +600% and highest trace latency in
`payment-service`; no error logs in window." When the top candidate *does* have a
dominant error cluster (the logs-only common case), rendering is exactly as
today. The logs-only path (no ranker/model) still produces today's
cluster-based `EvidencePacket` unchanged.

## Confidence = calibrated P(top-1 correct), not raw `predict_proba` (point 4)

A binary classifier's `predict_proba` per candidate is **not** a distribution
over mutually-exclusive services, and even per-candidate calibration answers the
wrong question. What confidence needs is **P(the selected top-1 is correct)**.
So separate the two stages:

```
ranking_score(service)  →  select top-1  →  confidence calibrator  →  P(top-1 correct)
```

The calibrator is trained **only on held-out top-1 predictions** (never the rows
the ranker trained on), from features like: top score, top1−top2 margin, number
of candidates, modalities available, and cross-modal agreement
(logs/traces/metrics pointing at the same service). Then "confidence 0.76" means
*predictions like this were right ~76% of the time* — which is what #83 has
wanted all along, and honestly earns the v1 schema's 0-1 `score`.

## Eval wiring

- `raglogs eval` / the `eval-corpus` workflow ingest traces+metrics for cases
  that have them, compute features, and score the ranker with **leave-one-system
  / -fault / -service-out** folds (the harness grows a `--holdout` dimension).
- Report per-fold top-1 vs the trivial baseline, exactly as the spike does.

## Phasing (each independently mergeable, each with an eval delta)

Revised per review to add **C0** (ground the number first) and **C1c**
(decouple the abstraction before wiring the ranker):

0. **C0** — commit the multi-modal spike that produced 48.9%: script, exact
   feature dataset, LOSO **and LOFO** (leave-one-fault-out) results, the oracle
   ceiling, and the **modality-ablation** table (does the lift survive without a
   missingness shortcut?). *In progress on this PR.*
1. **C1a** — telemetry canonical model + migration + trace/metric adapters +
   RCAEval converter, incl. the `attributes` escape hatch. *No RCA behavior
   change.* Eval delta: none.
2. **C1b** — `features.py` + a `raglogs rca-features` debug command; reproduce C0
   *through the pipeline*, with explicit modality-presence features.
3. **C1c** — introduce `RootCauseCandidate`; decouple RCA identity from the log
   cluster. Still no learned ranker (a deterministic scorer is fine here). Keeps
   C2 from accreting glue around `select_primary_cluster`.
4. **C2** — wire the ranker (non-pickle JSON artifact, graceful fallback);
   **require lift on both RE2 and RE3** (a ranker that lifts RE3 but regresses
   RE2 — the #117 shape — does not ship). Eval delta: the LOSO lift.
5. **D** — calibrate **P(top-1 correct)** separately from ranking (#83).
6. **E** — (optional) LLM *re-ranks* the top-k candidates' evidence; never raw
   telemetry.

## Open questions for review

- **Model in-repo vs fetched?** Resolved (review): **in-repo, as a non-pickle
  JSON artifact** — tiny, versioned, diffable, and safe to load (no code
  execution). See the Ranker section.
- **Metric anomaly quality.** The spike's `met_anom` is a crude mean-change;
  a robust change-point detector (BARO-style) likely lifts the number. Ship the
  *exact crude form the C0 spike used* first (so C1b reproduces the number),
  improve behind the eval.
- **RE2 before C2 — non-negotiable.** Run RE2 through the ranker before wiring
  C2, so the lift is stated on both corpora; a ranker that lifts RE3 but
  regresses RE2 (the #117 shape) does not ship. Now explicit in the phasing.

_Part of #118 / #74. Supersedes nothing; implements the pivot in `docs/rca-direction.md`._
