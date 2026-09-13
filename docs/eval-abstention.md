# Abstention gate — calibration & evaluation (#79)

Measured result behind the abstention gate designed in `docs/design-abstention.md`
and implemented in `src/core/rca/abstention.py`. Calibrated with
`scripts/calibrate_abstention.py` on RCAEval (RE3 + RE2), **nested
leave-one-system-out** so the recall-constrained operating point is not overfit.

## Method

Per case, two labelled 300 s windows (as in the spike): an **incident** window
`[inject, inject+W]` (baseline `[inject−W, inject]`) and a genuinely-healthy
**pre-injection** window `[inject−W, inject]` (baseline `[inject−2W, inject−W]`).
Each modality → a stabilized, clamped effect size → fixed saturating transform →
`[0,1]`; fuse by `max` over available modalities (the product code in
`abstention.py`). Nested LOSO: choose `τ_*` + threshold on the training systems
under **incident recall ≥ 99%**, maximising healthy abstention; measure on the
held-out system; rotate. Report **held-out** numbers.

## The trace-arm ablation (the decisive experiment)

The first run flagged that the `L+T+M` (logs+traces+metrics) availability pattern
abstained far worse than `L+M`. To separate "traces are a bad detector" from
"traces-present cases are just harder systems" (the patterns are confounded with
system), re-run identical nested LOSO on the **same** cases with the trace arm
removed from fusion — `max(log, metric)` vs `max(log, metric, trace)`, nothing else
changed:

| suite | pattern | with traces | **without traces** |
|---|---|---|---|
| RE3 | `L+T+M` abstention | 14/46 (30%) | **21/46 (46%)** |
| RE2 | `L+T+M` abstention | 44/88 (50%) | **78/88 (89%)** |
| RE3 | overall abstention / recall | 55.6% / 93.3% | **63.3%** / 93.3% |
| RE2 | overall abstention / recall | 71.5% / 97.8% | **77.4%** / 96.3% |

Removing traces lifts `L+T+M` abstention substantially **at equal-or-marginally-lower
recall, on the identical cases** — a causal result, not the confounded correlation.
The trace **span-rate ratio swings with normal traffic**, so it is a noisy
*fault-vs-no-fault* detector: it floods the `max` fusion on healthy windows. (Its
value is in **localisation** — the RCA ranker, #118 — not detection.) The only
pattern helped by traces is `T+M` (no logs), and metrics-alone still holds ~100%
recall there.

**Decision: the Gen-2 abstention gate uses logs + metrics only; traces stay in the
ranker.** This yields the architecture:

```
INCIDENT DETECTION      logs + metrics            → abstain / proceed
ROOT-CAUSE LOCALISATION logs + metrics + traces   → rank services
```

## Frozen defaults (logs + metrics, RE3 + RE2 combined)

Re-fit on all development data (both suites), the shipped constants:

```
tau_log = 0.25    tau_metric = 4.0    threshold = 0.377
```

Held-out (nested LOSO, 720 windows = 360 incident + 360 healthy):

| | recall | healthy abstention |
|---|---|---|
| **overall** | **96.9%** (349/360) | **78.9%** (284/360) |
| `L+M` | 120/120 (100%) | 103/120 (86%) |
| `L+T+M` (gate ignores T) | 151/161 (94%) | 95/134 (71%) |
| `T+M` (metrics only) | 78/79 (99%) | 86/106 (81%) |

## Honest caveats

- **The recall floor does not fully transfer.** A threshold giving ≥99% recall on
  the training systems yields **~97% held-out** (RE3 93.3%, RE2 96.3%, combined
  96.9%). This is exactly what nested LOSO exists to expose — a threshold picked and
  scored on the same cases would look better and be a lie. ~3% of real incidents
  would be (wrongly) abstained at this operating point; that is the cost of the
  healthy-window rejection, and the threshold is the knob (lower ⇒ higher recall,
  less abstention). Opt-in/default-off means no one inherits this silently.
- **Pre-injection windows are a healthy proxy**, not real production-quiet traffic;
  the frozen gate still needs validation on a fresh external corpus before default-on.
- **`W = 300 s` fixed, single split.** Systems `ob/ss/tt` shared across suites.
- **Window-size transfer is unmeasured.** `τ`/threshold were frozen at *symmetric*
  ~300 s incident+baseline windows. In the pipeline the incident window is the
  user's query window and the baseline defaults to `24h` — a different regime. The
  arms are rate/magnitude-normalized so they are *partly* scale-invariant, but the
  threshold's behaviour at arbitrary window sizes is not measured here. Before
  default-on, validate at the production default window sizes (or scale the gate's
  baseline toward the calibrated ~300 s regime). The gate is opt-in precisely so
  this is a deliberate choice, not a silent default.
- **The gate judges the explained view.** `compute_window_anomaly` filters by
  `service` / `environment` / `ingestion_job_id` like the rest of `explain_window`,
  so a job-scoped run (the CLI norm) does not mix other ingests' error counts into
  the incident/baseline comparison (no cross-job baseline pollution).

_Part of #79 / #118 / #74. Numbers 2026-09-11. Gate = `max(log, metric)`;
`τ_log 0.25 / τ_metric 4.0 / threshold 0.377`; held-out recall 96.9% / abstention
78.9% over RE3+RE2._

## Gen-3.1 recalibration — hierarchical metric detector (2026-09-12)

The external validation (`docs/eval-otel-demo.md`) then showed the original metric
arm (relative mean-change, `max` over all metrics) **saturated to 1.0 on healthy
OTel**: raw OTLP cumulative counters compared by windowed mean grow with time, and
`max` over ~27k metrics always finds a twitch. The metric arm was redesigned
(`metric_semantics`, #163/#165) into a **hierarchical detector**: per metric a stable
log-space anomaly `saturate(|log1p(q_inc) − log1p(q_base)|, τ)` (`q` = rate for
counters/histograms, level for gauges; no divide-by-tiny-baseline); per service a
**corroborated top-k** (score only when ≥`k` metrics clear a threshold `T`, then mean
of the top-k) so a lone metric of thousands can't flag a service; then `max` across
services.

Recalibrated on RCAEval RE3+RE2 (nested LOSO, recall floor 99%), searching
`τ_log × τ_metric × k × T`. **Frozen Gen-3.1 defaults:**

```
tau_log = 0.25   tau_metric = 0.25   k = 2   corroboration_threshold = 0.3   threshold = 0.407
```

Held-out (720 windows = 360 incident + 360 healthy):

| | recall | healthy abstention |
|---|---|---|
| **overall** | **93.9%** (338/360) | **58.6%** (211/360) |
| `L+M` | 276/281 (98%) | 113/254 (44.5%) |
| `M` (metrics only) | 62/79 (78%) | 98/106 (92%) |

The recall floor (99% on train) again transfers to ~94% held-out — the nested LOSO
being honest. **Still opt-in / default-off:** these are RCAEval-calibrated but not
yet **externally** re-validated on a fresh **typed** OTel corpus. That is the next
step (now that #164 round-trips `metric_type`): confirm the metric arm no longer
saturates on healthy OTel — the failure this whole cycle targets — before any
default-on. Reproduce with `scripts/calibrate_abstention.py --suites re3,re2`.

_Numbers 2026-09-12. Gate = `max(log, hierarchical-metric)`, logs+metrics only._
