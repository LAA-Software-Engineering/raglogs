# Structural shadow evaluation — measuring the A–E treatment (Phase H2, #186)

Phase H1 measured the *disease* (which failure class dominates the existing pipeline; recorded on
#177). This is the *treatment*: run the A–E structural core (#178–#183) on **real** eval telemetry,
**offline / in shadow**, and score its five outcomes against ground truth — does the structural
machinery retain (and sometimes uniquely pin) the causes the log-cluster pipeline misses? It changes
**nothing** in the product explain path.

## What it does (`src/eval/structural_shadow.py`)

For each labeled positive case, purely from its own `metrics.jsonl` + `spans.jsonl` (no DB):

- **Observables** — `sig:{service}` = `PRESENT` when the service is anomalous this incident (error rate
  present, or latency ≥2× baseline, discretized with the Phase D policy), else `ABSENT`; `OBSERVED`
  because we measured it. A service with no metrics stays UNKNOWN — exactly how A–E treats missing
  telemetry.
- **Hypotheses** — one `process` hypothesis per candidate service. Candidates are the anomalous
  services plus, when a service's fault is **not** already explained by a visible (anomalous) callee,
  its silent callees — so a silent downstream root is still generated (the coverage gap), without a
  healthy sibling callee polluting a case whose root emits its own error. Each hypothesis predicts its
  own `sig` present and is **hard-contradicted** by a failing callee (a failing callee means it is not
  the root).

Then `partition → resolve → outcome`, scored as `struct_ok` (truth retained in a surviving class),
`unique` (IDENTIFIED and the sole localization is the truth), and `abstained`
(NO_COMPATIBLE_HYPOTHESIS).

Run it: `python scripts/structural_shadow_eval.py data/eval-cases/trace-loc`.

## Result — synthetic trace corpus (`trace-loc`, #170, 24 cases)

| | struct_ok | unique |
|---|---|---|
| **overall** | **100%** | **50%** |
| `callee_fail` (root emits its own error) | 6/6 | 6/6 → `IDENTIFIED` |
| `latency_only` (root latency spikes) | 6/6 | 6/6 → `IDENTIFIED` |
| `symptom_only` (root is silent) | 6/6 | 0/6 → `UNCERTAIN`, truth retained |
| `caller_fail` | 6/6 | 0/6 → `UNCERTAIN`, truth retained |

Against the **same corpus** under H1's existing-pipeline taxonomy (33% top-1 correct; failures split
inference 62.5% / coverage 37.5%):

- The `callee_fail` / `latency_only` families H1 scored as **inference** (loud caller outranks the root)
  become **`IDENTIFIED` at the true root** — the propagation hard-rule eliminates the callers.
- The `symptom_only` roots H1 scored as **coverage** (never generated) are **retained** as an honest
  **`UNCERTAIN`** — the silent root can't be uniquely separated from its symptom service, which is the
  correct structural answer, not a miss.

So on clean telemetry A–E lifts truth-retention from 33% → **100%** and uniquely resolves exactly the
half of cases where the root emits a signal. **Structural inference is validated where the observable
signal exists.**

## Result — real OTel-Demo (`otel`, `otel-fresh`, 9 cases each): `no_candidates`

The minimal adapter produced **no candidates** on every OTel case, because the telemetry is not
per-service error/latency: metrics are OTLP infra counters (`process.cpu.time`,
`container.memory.percent`, `nginx.connections_*`) — many with `service = null` — and the sampled spans
carry `status_code = null`. There is no clean anomaly signal for the minimal `sig:{service}` adapter to
key on.

This is **not** a failure of the structural core; it localizes the real-world gap to the **observable
adapter** — deriving per-service error/latency/saturation signals from messy OTLP + span telemetry.
That is the same standing OTel lesson (candidate generation is the OTel blocker;
`project_otel_frozen_external_validation`), now pinned to the observable-derivation layer rather than
the partitioning.

## Read for the checkpoint

- **A–E is validated on clean telemetry** — a large, honest lift over the existing pipeline (truth
  retained 33% → 100%; unique 50%) with no ranker and no score-gap heuristic.
- **Its real-world value is gated on a faithful Observable adapter** from OTLP/span telemetry — a
  Phase F–adjacent effort (absence/saturation-derived signals), not more structural machinery.
- The synthetic result should not be over-read: clean topology and injected faults. The next honest
  step is the OTLP observable adapter, then re-run this shadow eval on OTel and RCAEval RE3.

Caveats: the causal model here is a deliberate minimal v1 (single `sig` family + callee-propagation
hard rule); its purpose is to exercise A–E end-to-end and produce comparable outcomes, not to be a
tuned production model.
