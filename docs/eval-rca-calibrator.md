# RCA confidence calibrator — reliability (#118 D / #83)

The ranker's `top_score` is not P(the top-1 service is correct). #83 is the
long-standing observation that raglogs' confidence is anti-calibrated, so
confidence should be a *separate* estimator over the ranked distribution,
predicting **P(top-1 correct)**.

`scripts/eval_rca_calibrator.py` measures reliability with **nested
leave-one-system-out** — no leakage for either model:

1. **Out-of-fold ranker.** For each held-out system, a ranker trained on the
   other systems ranks its cases; per case we record the ranked-distribution
   features and the label `top-1 correct`.
2. **Out-of-fold calibrator.** For each held-out system, a calibrator trained on
   the other systems' stage-1 rows predicts P(top-1 correct) on the held-out
   rows, compared against the naive baseline of the raw `top_score`.

Metric: expected calibration error (**ECE**, 10 bins) — lower = more honest.

## Model choice is measured

Candidate calibrators, nested LOSO ECE:

| calibrator | RE3 ECE | RE2 ECE |
|---|---|---|
| raw ranker `top_score` (no calibration) | 0.422 | 0.420 |
| gradient boosting over all 5 features | **0.502** | 0.374 |
| logistic over all 5 features | 0.333 | 0.114 |
| **Platt (logistic on `top_score`)** | **0.202** | **0.073** |
| constant base rate (reference) | 0.089 | 0.070 |

The flexible calibrators **overfit out-of-system** — gradient boosting makes RE3
*worse* than no calibration (0.42 → 0.50), producing confident-but-wrong
predictions. The extra distribution features (`margin`, `cross_modal_agreement`,
…) add out-of-system noise: logistic-on-all-5 is worse than logistic-on-`top_score`
alone on both corpora. **Platt scaling on `top_score`** roughly halves ECE on
both corpora and generalises to held-out systems, so it is the shipped calibrator:
`P(top-1 correct) = sigmoid(a·top_score + b)`.

That a *constant base rate* is the best-calibrated on RE3 (0.089) is itself the
#83 finding restated: on RE3 the ranker's `top_score` is **near-uninformative
about correctness** (the 0.9–1.0 confidence bin is only ~57% correct, ≈ the 61%
base rate). Platt scaling degrades gracefully toward the base rate exactly where
the score carries no signal, which is why it is safe; a high-capacity model does
the opposite. RE2 is more informative (top-1 accuracy 77%), and Platt tracks it
to ECE 0.073.

## Status

The calibrator is **default-off** (`rca_calibrator_model_path` empty →
`load_calibrator` returns `None` → the existing ordinal confidence is used). This
PR lands the measurement, the corrected Platt calibrator, and the trainer
(`scripts/train_rca_calibrator.py`, which fits Platt on out-of-fold pairs). Wiring
the calibrated confidence into `explain` (surfaced alongside `predicted_root_cause`)
is the next slice.
