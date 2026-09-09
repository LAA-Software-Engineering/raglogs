# RCA ranker — leave-one-*-out evaluation (#118 C2b)

`scripts/eval_rca_ranker.py` evaluates the learned multi-modal ranker with the
leakage discipline #118 requires — never a random split. Crucially, unlike the
exploratory spike (which scored sklearn directly), every prediction here routes
through the **committed** ranker code: each fold trains a
`GradientBoostingClassifier`, serialises it with `serialize_gbc`, and scores
held-out candidates with `RcaRanker.score_vector` (the pure-Python evaluator the
runtime uses). A green number is therefore evidence the *shipped artifact path*
reproduces the lift, not merely that sklearn can.

```bash
python scripts/eval_rca_ranker.py --features mm_features.jsonl --suite re3
```

## Result — RE3 (90 code-fault cases, 3 systems)

Baseline = trivial logs-volume top-1 **28.9%** (see [rca-benchmark.md](rca-benchmark.md)).
Oracle ceiling (truth is among the candidate services) = 100%.

| held-out axis | top-1 | vs baseline | per-group |
|---|---|---|---|
| **leave-one-system-out** | **60.0%** (54/90) | **+31.1pp** | ob 17/30 · ss 21/30 · tt 16/30 |
| leave-one-fault-out | 81.1% (73/90) | +52.2pp | f1 20/26 · f2 13/13 · f3 18/26 · f4 16/19 · f5 6/6 |
| leave-one-service-out | 60.0% (54/90) | +31.1pp | adservice 7/9 · orders 9/9 · ts-auth 13/15 · ts-route 13/15 · emailservice 9/15 · front-end 3/9 · carts 0/12 · cartservice 0/3 · currencyservice 0/3 |

Leave-one-**system**-out — train on two systems, test on a third the model has
never seen — is the headline: **60.0%, beating the baseline on every held-out
system**, and reproducing the spike's number exactly through the committed
evaluator. Leave-one-**fault**-out is easier (81%): fault classes share a
signature across services. Leave-one-**service**-out is the strictest; it holds
overall at 60% but collapses to 0 on some held-out services (`carts`,
`cartservice`, `currencyservice`) the model never saw as positive — a caveat for
services with idiosyncratic signatures.

## Status / next

This validates the **shipped ranker code** on RE3. Before the ranker is wired
into `explain` (C2b-wire), the same must be measured on **RE2** (resource/network
faults — a second independent corpus, expected to be even more metric-driven),
since the freeze bar is lift on two independent corpora. RE2 needs its telemetry
feature table extracted (the spike only built RE3); that extraction + the RE2
number is the immediate follow-up. The eval here is a measurement tool only — no
product path changed.
