# SPIKE result: multi-modal learned ranker for RE3 (#118, Phase C/D)

**Question (the one that gates the build):** the single-modality spikes each won
a *different* system (logs → online-boutique self-contained faults; traces →
train-ticket propagation faults; sock-shop has only logs+metrics). Can a model
fed **all three modalities** learn to pick the root cause *without knowing the
fault class* — and does it hold on a **system it never trained on**?

**Method:** `scripts/spike_multimodal_rca.py` — standalone, no `src/core`. Build a
per-`(case, candidate service)` feature table over incident `[inject, inject+600]`
vs baseline `[inject-300, inject]`:

| feature | source |
|---|---|
| `log_err` | error-level log lines for the service |
| `log_grp` | largest `(service, fingerprint)` error group |
| `log_stack` | originating stack-trace lines |
| `tr_rate` | trace span-rate ratio (0 if no traces) |
| `tr_dur` | trace p95-duration ratio |
| `met_anom` | max per-metric change ratio (`{service}_{metric}` columns) |

label = 1 if the service is the injected root cause. A `GradientBoostingClassifier`
(sklearn defaults, no tuning) ranks the candidate services per case; top-1 is the
prediction. Evaluated **leave-one-system-out** (train on two systems, test on the
held-out third).

## Result — RE3, 90 cases, leave-one-system-out

| held-out system | top-1 |
|---|---|
| online-boutique | 56.7% (17/30) |
| sock-shop | 50.0% (15/30) |
| train-ticket | 40.0% (12/30) |
| **overall** | **48.9% (44/90)** |

vs the logs-only volume baseline **28.9%** → **+20pp**, and it beats the baseline
on **every held-out system it never trained on**.

Feature importances (full-data fit): `met_anom 0.40`, `log_grp 0.28`,
`log_err 0.13`, `log_stack 0.11`, `tr_dur 0.05`, `tr_rate 0.04`. Genuinely
multi-modal and metric-led — no single modality carries it, and logs alone
(which capped at ~29%) never could.

## What this establishes

- **The multi-modal pivot works, and it generalizes.** The routing-without-a-
  label concern is answered: the learned ranker combines modalities and holds
  out-of-distribution (leave-one-system-out), rather than memorising per-system
  quirks.
- **Metrics are the largest single signal** on RE3, then log error-grouping and
  stack content; traces contribute at the margin (they were decisive on
  train-ticket specifically). All three matter.
- This is the empirical green light to build the real thing: ingest traces +
  metrics (freeze carve-out already in `AGENTS.md`), compute these per-service
  features in the pipeline, and rank candidates — feeding calibrated confidence
  (#83) from `P(root cause)`.

## Honest caveats

- 90 cases / 3 systems / 3 LOSO folds — small; the 40–57% spread shows real
  variance. All folds beat baseline, but the point estimate will move with more
  data (the OTel corpus #79 is the natural third-party check).
- RE3 (code faults) only. RE2 (resource/network) is expected to be *even more*
  metric-driven; not yet run through the ranker.
- `met_anom` is a crude mean-change feature; a real implementation would use
  proper change-point/robust anomaly detection (BARO-style) and likely do
  better.
- No hyperparameter tuning (deliberately — avoids fitting to a validation set).

_Part of #118 / #74. Numbers 2026-09-08._
