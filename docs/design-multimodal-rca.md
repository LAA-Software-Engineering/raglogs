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
  operation, start_time, duration_ms, status_code, ingestion_job_id`. Indexed on
  `(scope, service, start_time)` and `(scope, trace_id)`.
- `metric_samples`: `id, scope, service, metric, value, ts, ingestion_job_id`.
  Indexed on `(scope, service, metric, ts)`. (Long format; the RCAEval
  `{service}_{metric}` wide columns are melted on ingest.)

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

Missing-modality features are `0.0`; the ranker was trained with that encoding,
so logs-only inference is in-distribution. Baseline window reuses
`resolve_baseline_window` + the in-job baseline (#115).

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

## Integration with explain / confidence

- `assemble_evidence` gains an optional ranker: when a model + any non-log
  modality is present, `select_primary_cluster` is replaced by "primary = cluster
  of the top-ranked service"; otherwise unchanged. Attribution and evidence
  narrative are unchanged (still human-readable).
- **When the top-ranked service has no error cluster** (a resource/network fault
  can surface only in traces/metrics — e.g. a CPU-hogged service with elevated
  latency but no error logs): map it to the service's *highest-importance* cluster
  of any level (warn/info) if one exists; else **synthesize a primary evidence
  item from the winning modality** — e.g. "elevated p95 latency / CPU in
  `svc` (from traces/metrics); no error logs in window" — rather than forcing a
  fabricated error cluster. The `EvidencePacket` gains an optional
  `primary_service` + `primary_signal` so the explanation can name a root cause
  that logs alone never surfaced. This is the whole point of going multi-modal;
  the narrative stays honest about *which* signal implicated the service.
- **Confidence (#83) becomes real:** the ranker's `P(root cause)` is a genuine
  calibrated probability (reliability-curve calibrated on held-out folds),
  replacing the ordinal placeholder. This is the honest 0-1 the v1 schema always
  wanted.

## Eval wiring

- `raglogs eval` / the `eval-corpus` workflow ingest traces+metrics for cases
  that have them, compute features, and score the ranker with **leave-one-system
  / -fault / -service-out** folds (the harness grows a `--holdout` dimension).
- Report per-fold top-1 vs the trivial baseline, exactly as the spike does.

## Phasing (each independently mergeable, each with an eval delta)

1. **C1a** — models + migration + trace/metric adapters + RCAEval converter.
   *No behavior change* (data ingested, unused). Eval delta: none.
2. **C1b** — `features.py` + a standalone `raglogs rca-features` debug command.
   Reproduce the spike's numbers *through the pipeline* (not the ad-hoc script).
3. **C2** — `ranker.py` + offline training script + wire into `assemble_evidence`
   behind graceful fallback. Eval delta: the LOSO lift (target: reproduce ~49%).
4. **D** — calibrated confidence from `P` (#83), leave-one-*-out reliability.
5. **E** — (optional) LLM *re-ranks* the top-k candidates' evidence; never raw
   telemetry.

## Open questions for review

- **Model in-repo vs fetched?** Resolved (review): **in-repo, as a non-pickle
  JSON artifact** — tiny, versioned, diffable, and safe to load (no code
  execution). See the Ranker section.
- **Metric anomaly quality.** The spike's `met_anom` is a crude mean-change;
  a robust change-point detector (BARO-style) likely lifts the number. Ship
  crude first (reproduce the spike), improve behind the eval.
- **RE2.** Not yet run through the ranker; expected even more metric-driven.
  Run it before C2 merges so the lift is stated on both corpora.

_Part of #118 / #74. Supersedes nothing; implements the pivot in `docs/rca-direction.md`._
