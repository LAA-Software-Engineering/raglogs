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

Oracle ceiling (the true service is a candidate at all): **100%** — no
candidate-generation loss. Modality ablation (each row is a full LOSO run):

| features | overall | online-boutique | sock-shop | train-ticket |
|---|---|---|---|---|
| logs-only | 20.0% | 6/30 | 7/30 | 5/30 |
| logs + traces | 31.1% | 6/30 | 13/30 | 9/30 |
| logs + metrics | 45.6% | 17/30 | 23/30 | 1/30 |
| all, no presence flags | 48.9% | 17/30 | 15/30 | 12/30 |
| **all + modality-presence flags** | **60.0%** | 17/30 | 21/30 | 16/30 |

vs the logs-only volume baseline **28.9%**. The winning variant **beats the
baseline on every held-out system it never trained on** (57% / 70% / 53%).

Reading the ablation:

- **Complementary by system:** metrics carry online-boutique + sock-shop
  (`logs+metrics` 17/23), traces carry train-ticket (`logs+traces` tt 9 vs
  metrics' 1). No single modality wins everywhere.
- **Presence flags matter a lot (ChatGPT review point 2, confirmed).** Without
  them, adding traces *hurt* sock-shop (23→15): `tr_rate = 0` there means "no
  traces", and the model couldn't tell that from "no anomaly". Adding
  `has_logs/has_traces/has_metrics` recovered sock-shop (→21) and lifted overall
  **48.9% → 60.0%**. The number the design was anchored on was *suppressed* by
  the missingness ambiguity; fixing it is a +11pp gain, not just a de-risk.
- Feature importances (all, no presence): `met_anom 0.40`, `log_grp 0.28`,
  `log_err 0.13`, `log_stack 0.11`, `tr_dur 0.05`, `tr_rate 0.04` — metric-led,
  genuinely multi-modal; logs alone capped at ~29%.

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

- 90 cases / 3 systems / 3 LOSO folds — small; per-system counts (17–21/30)
  are noisy. All folds beat baseline, but the point estimate will move with more
  data (the OTel corpus #79 is the natural third-party check).
- RE3 (code faults) only. RE2 (resource/network) is expected to be *even more*
  metric-driven; not yet run through the ranker — **required before C2 wires in**.
- `met_anom` is a crude mean-change feature; a real implementation would use
  proper change-point/robust anomaly detection (BARO-style) and likely do
  better. Ship the crude form first so C1b reproduces this number.
- No hyperparameter tuning (deliberately — avoids fitting to a validation set).
- `predict_proba` here is a *ranking score*, not calibrated `P(top-1 correct)` —
  confidence calibration is a separate Phase D step (see the design doc).

## Gen-2 attempt: call-direction feature — NEGATIVE (2026-09-11)

The frozen OTel-Demo run (#79, `docs/eval-otel-demo.md`) showed the ranker's top-1
gap is the *propagation* signature: it ranks the busy **caller** (e.g. `checkout`)
over the true culprit **callee** (`payment`) it depends on. So the obvious gen-2
feature is a **call-direction** score `tr_calldir` per candidate service: from the
caller→callee trace graph (`src/core/rca/linkage.ServiceGraph`), weighted by
log-error volume,

  `tr_calldir = (erroring_callers − erroring_callees) / (sum + eps)  ∈ [−1, 1]`

`+1` = every failing neighbour *depends on* the service (propagation origin);
`−1` = it only *calls* failing downstream services (symptom). Added as the
`all + presence + calldir` ablation (`_call_direction` + graph columns in
`_trace_features`).

**Result — a wash on RE3 LOSO:**

| variant | overall | ob | ss | tt |
|---|---|---|---|---|
| all + presence | **63.3%** | 17/30 | 24/30 | 16/30 |
| all + presence + calldir | **63.3%** | 17/30 | 25/30 | 15/30 |

The feature is *directionally correct* — true root causes score a mean **+0.5**
when traces exist (`tt`/`ob`; `ss` has no traces so it is always 0), never
negative — and separable (positives mean `tr_calldir` 0.344 vs 0.041 for
non-causes). But it yields **no top-1 lift**: on RCAEval the propagation
information it encodes is already carried by the trace-rate and metric-anomaly
features the ranker uses, so it is redundant (the ±1-case per-system move is noise
from a 10th feature perturbing tree splits). It also fires on only ~⅓ of positives
because it weights by *log* errors, which the trace-only propagation faults lack.

**Decision:** held as a negative result — **not** shipped into
`src/core/rca/features.py` / `FEATURE_NAMES` / the model artifact (kept only as
this reproducible spike ablation), same disposition as the earlier onset/novelty
washes. It targets a failure mode (OTel top-1) that is *frozen for model
selection*, so a wash on the RCAEval dev corpus is the stop signal. Revisit only
if a future external corpus shows the existing trace/metric features fail to
capture propagation there.

_Part of #118 / #74. Numbers 2026-09-09. Headline: **60.0% RE3 LOSO** with
modality-presence flags (48.9% without); logs-only baseline 28.9%._
