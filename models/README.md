# Frozen RCA model artifacts (#118 / #79)

These are the **frozen** multi-modal RCA artifacts for the #79 external-validation
experiment. They are committed (small, non-pickle JSON) precisely so the freeze is
git-auditable: the timestamp and content are fixed *before* any independent-corpus
(OTel Demo) result is seen, making `Model changes after capture: 0` verifiable.

| file | what |
|---|---|
| `rca_ranker.json` | gradient-boosted tree ensemble, `P(service is root cause)` from the 9 multi-modal features |
| `rca_calibrator.json` | Platt scaling on the ranker `top_score`, `P(top-1 correct)` |

## Provenance

- **Trained on:** RCAEval **RE2 + RE3** — 359 labeled cases, 11,420 `(case, service)` rows.
- **Ranker:** `GradientBoostingClassifier` (features in `src/core/rca/features.py::FEATURE_NAMES`), serialised via `scripts/train_rca_ranker.py` (self-checks pure-Python↔sklearn parity, `max_err ≈ 9e-16`).
- **Calibrator:** Platt scaling fit on **out-of-fold** (leave-one-system-out) ranker predictions, `scripts/train_rca_calibrator.py` (`a=-1.205, b=1.296`). The negative slope is honest: across held-out systems the ranker's `top_score` is only weakly informative about correctness, so the calibrator degrades toward the base rate rather than inventing confidence (the #83 finding; see `docs/eval-rca-calibrator.md`).
- **Validated (leave-one-system-out, RCAEval):** ranker top-1 **RE3 60.0% / RE2 77.0%**, beating the trivial baseline (28.9% / 8.0%) on every held-out system (`docs/eval-rca-ranker.md`).
- **OTel cases seen in training: 0.**

## Reproduce

```bash
# 1. Feature tables (RCAEval telemetry -> per-(case,service) features)
python scripts/spike_multimodal_rca.py --extract --suites re2,re3 --out mm_features_re2re3.jsonl
# 2. Train + freeze
python scripts/train_rca_ranker.py     --features mm_features_re2re3.jsonl --out models/rca_ranker.json
python scripts/train_rca_calibrator.py --features mm_features_re2re3.jsonl --out models/rca_calibrator.json
```

## Use

**Default-off.** Nothing loads these unless a deployment points at them explicitly
(`RCA_RANKER_MODEL_PATH` / `RCA_CALIBRATOR_MODEL_PATH`, or the `rca_ranker_model_path`
/ `rca_calibrator_model_path` settings). The intended use is the frozen external
validation:

```bash
raglogs frozen-eval data/eval-cases/otel \
    --ranker models/rca_ranker.json --calibrator models/rca_calibrator.json
```

Do **not** retrain or retune after seeing OTel results — that turns external
validation into training on a third corpus (see `docs/eval-otel-demo.md`).
