# OTel-Demo incident corpus + frozen external validation (#79)

RCAEval gave raglogs its first real numbers, but the whole design has now seen
it — candidate features, model selection, presence flags, and even repeated
leave-one-system-out all draw train and test from the *same benchmark universe*.
A 60% RE3 / 77% RE2 result can be entirely real and still degrade on a genuinely
different deployment. The biggest remaining unknown is **external validity**.

The [OpenTelemetry Demo](https://opentelemetry.io/docs/demo/feature-flags/) is the
independent corpus for that: it is a different microservice system, its failures
are triggered by runtime feature flags (flagd/OpenFeature) with no redeploy, and
the ground truth is correct **by construction** because we cause the fault and
record its exact time. It also exercises what RCAEval cannot — **deploy-class**
regressions, healthy **negative** cases, and **confounded** cases.

## Generating cases

`scripts/eval/generate_incident.py` runs one loop: **baseline window → flip flag
(record exact inject time) → incident window → emit `case.yaml` (+ telemetry
sidecars) → flip back**. Telemetry comes from the demo's bundled OTel Collector:
add a `file` exporter that writes OTLP-JSON to `./capture/{logs,traces,metrics}.json`
(Prometheus/Jaeger/Grafana ship with it — no SaaS backend). `src/eval/otlp.py`
converts that OTLP-JSON into the harness records (`logs.jsonl` +
`ParsedSpan`/`ParsedMetricSample` sidecars), window-filtered — the same shapes the
RCAEval path and the multi-modal feature layer already consume, so the frozen
model runs on identical inputs.

```bash
# positive case
python scripts/eval/generate_incident.py --flag paymentServiceFailure \
    --flagd-url http://localhost:8080 --otlp-dir ./capture \
    --out data/eval-cases/otel/payment_1

# healthy negative (raglogs must abstain)
python scripts/eval/generate_incident.py --negative \
    --otlp-dir ./capture --out data/eval-cases/otel/healthy_1
```

Built-in failure flags → root cause (`src/eval/otel_demo.py::FLAG_SCENARIOS`):
`productCatalogFailure`, `paymentServiceFailure`, `cartServiceFailure`,
`recommendationServiceCacheFailure`, `kafkaQueueProblems`, `adHighCpu`,
`imageSlowLoad`, `loadGeneratorFloodHomepage`. Verify each scenario's `service`
matches the running demo's `service.name` (it has changed across demo versions).

Case classes to generate:

- **Flag cases** — one per built-in failure flag.
- **Deploy-class cases** — roll the demo from image tag A → B where B carries an
  injected regression, so the deploy line and the errors are causally linked and
  the deploy timestamp is the true trigger. (These don't exist in any public set.)
  `otel_demo.generate_deploy_incident(..., roll=<kubectl set image / compose>)`
  runs the loop and emits a `trigger.type: deploy` case.
- **Negative cases** — load generator running, no fault; `expect_explanation:
  false`. A tool that invents an incident from healthy logs is worse than useless
  on-call.
- **Confounded cases** — flip a flag *and* perform an unrelated deploy in the same
  window; the ground-truth trigger stays the flag flip. Separates causal reasoning
  from "the most recent trigger-looking line wins". Pass `generate_incident(...,
  confounder=<unrelated action>, confounder_after=<s>)`; the distractor is recorded
  in the case notes.

For infrastructure faults flags can't express (network partitions, pod kills, I/O
faults), layer [Chaos Mesh](https://chaos-mesh.org/docs/) (e.g. a `NetworkChaos`
partition between cart and Redis; see Coroot's OTel-Demo + Chaos-Mesh recipe).
`otel_demo.generate_chaos_incident(..., apply_chaos=/delete_chaos=<kubectl
apply/delete -f>)` runs the loop over a `ChaosScenario` (`CHAOS_SCENARIOS` has
representative NetworkChaos / PodChaos / IOChaos / StressChaos faults) and always
deletes the experiment on the way out. Generated cases are small enough to commit
as permanent regression fixtures.

## The experiment: frozen external validation

The point of this corpus is **not** to retrain on it. Train on RCAEval, freeze
the exact artifact, and run it here untouched:

```
train:  RCAEval RE2 + RE3         (ranker + calibrator artifacts)
test:   OTel-Demo corpus          (never seen, independently generated)
NO:     fitting thresholds · retraining the ranker ·
        recalibrating confidence · hand-tuning features
```

```bash
# 1. Train + FREEZE the artifacts on RCAEval (before generating/seeing OTel cases)
python scripts/train_rca_ranker.py      --features mm_features_re2re3.jsonl --out models/rca_ranker.json
python scripts/train_rca_calibrator.py  --features mm_features_re2re3.jsonl --out models/rca_calibrator.json

# 2. One frozen evaluation over the independent corpus.
# RCA_EXCLUDED_SERVICES drops non-service infra the ranker never trained against
# (traffic generator, flag daemon, ingress proxy) — they aren't root causes and
# otherwise dominate the ranking. --format json includes per-case predictions.
RCA_EXCLUDED_SERVICES=load-generator,flagd,frontend-proxy,image-provider \
raglogs frozen-eval data/eval-cases/otel \
    --ranker models/rca_ranker.json --calibrator models/rca_calibrator.json --format json
```

`raglogs frozen-eval` runs the frozen artifacts over the corpus and prints the
report below with an explicit provenance banner (`OTel cases seen in training: 0`,
`Model changes after capture: 0`), so the result is publishable and hard to later
misrepresent. **Freeze the ranker + calibrator before this run** — retuning after
seeing the corpus turns external validation into training on a third corpus, and
the synthetic unit fixtures exist only to test the plumbing/metric math, never to
tune anything.

Measure — top-1 is no longer the only thing that matters:

- **root-cause top-1 / top-3**;
- **abstention** on healthy negatives (did it correctly return insufficient
  evidence?);
- **calibrated-confidence reliability** (ECE) — RE3 already warned that a constant
  base rate beat the learned map, i.e. the score carries little correctness
  information out-of-system, so this is the number to watch before users read
  "78% confidence" literally;
- **trigger correctness** on deploy-class cases;
- **behaviour under confounding** (right trigger vs the distractor deploy);
- **per-fault-class** performance.

A large drop (say RE3 60% / RE2 77% → OTel 22%) is not a failure of the work — it
is the most important result available: it says raglogs has a good
*RCAEval-generalising* model, not yet a general RCA model, and #79 becomes the
training/eval material for the next generation. Only if the **frozen** model holds
up reasonably is there evidence to consider shipping a model on-by-default and
closing #118.

## First frozen result (2026-09-10, preliminary)

First end-to-end Path-A run: docker-compose demo, 9 flag + 2 negative cases,
**short 40/80 s windows**, single run (small corpus). Frozen RCAEval RE2+RE3
ranker + calibrator, untouched.

| metric | RCAEval (LOSO) | OTel-Demo (frozen) |
|---|---|---|
| root-cause top-1 | RE3 60% / RE2 77% | **0%** (0/9) |
| root-cause top-3 | — | 22% (2/9) |
| negative abstention | — | 0/2 |
| confidence ECE | RE2 0.07 / RE3 0.20 | **0.61** |

**The model does not transfer.** Per-case it ranks high-traffic / infra services
first — `load-generator`, `flagd`, `frontend-proxy`, `quote` — pushing the injected
service to #2–#4 or off the list (paymentFailure → `load-generator`; adHighCpu →
`quote`, truth `ad` at #2; productCatalogFailure → `flagd`, truth `product-catalog`
at #4). RCAEval has no load generator or `flagd`, so the ranker never learned to
discount a traffic driver — a real distribution gap. It is also badly
**overconfident** (~0.55 confidence at 0% accuracy → ECE 0.61) and never abstains
on healthy windows. This is the intended verdict of external validation: **a good
RCAEval-benchmark model, not yet a general RCA model.**

Caveats (why this is preliminary): short windows may not let faults fully manifest;
11 cases / single run; `kafka` can't match (infra, no `service.name`); propagation
faults (payment fails → checkout errors) mean the erroring service ≠ the injected
one. Next steps before a verdict: longer windows, more cases, and a candidate
filter that drops non-service infra (load-generator / flagd / proxy) — then re-judge
whether the gap is the model or the harness.

## Re-run with the candidate filter (2026-09-10)

The candidate filter now exists (`RCA_EXCLUDED_SERVICES`, #152). Re-running the
**same frozen artifacts over the same 11 cases** — the only change being
`RCA_EXCLUDED_SERVICES=load-generator,flagd,frontend-proxy,image-provider`, applied
at ranking time, no re-capture and no re-training — isolates how much of the miss
was infra distractors vs a real model gap:

| metric | frozen, no filter | frozen + filter |
|---|---|---|
| root-cause top-1 | 0% (0/9) | **0%** (0/9) |
| root-cause top-3 | 22% (2/9) | **44%** (4/9) |
| negative abstention | 0/2 | 0/2 |
| confidence ECE | 0.61 | 0.63 |

**The distractors accounted for half the top-3 gap, but not the top-1 gap.**
Dropping the traffic driver / flag daemon / ingress proxy doubles top-3 recall
(22% → 44%) — the injected service reaches the shortlist in twice as many cases —
which confirms the filter is a genuine improvement on unseen data, not just eval
hygiene. But **top-1 stays at 0%**: in every case where the truth now makes top-3,
it lands at position **#2 or #3, never #1** (adHighCpu → `ad` #2; productCatalog →
`product-catalog` #2; recommendationCache → `recommendation` #2;
loadGeneratorFlood → `frontend` #3).

The surviving misses skew toward the model ranking a high-traffic **caller** over
the true culprit — `paymentFailure`/`paymentUnreachable` (truth `payment`) rank
`checkout` / `cart` / `ad` first; `cartFailure` ranks `product-catalog` first. This
is the propagation-fault signature `docs/rca-direction.md` predicted: the exception
surfaces downstream of the injected service, and the ranker settles on the busy
neighbour rather than walking call-direction back to the origin — even though traces
are captured. So the residual gap is a **model/feature gap in top-1 precision**, not
merely distractor contamination.

Two caveats stand unchanged by the filter: **abstention is still 0/2** (both healthy
windows produce an explanation from load-generator background traffic — a real
false-alarm/calibration gap), and `kafka` remains unwinnable as posed (no
`service.name=kafka` in telemetry, so it can never be a candidate — a corpus fix,
not a model one).

**Verdict.** The filter is worth keeping (top-3 recall doubled, deployment-agnostic
default-empty per #81). It does **not** rescue top-1 on an unseen deployment — the
"good RCAEval-benchmark model, not yet a general RCA model" conclusion holds, and the
next generation needs call-direction features that survive leave-one-system-out, plus
an abstention path for healthy windows, before a model-on-by-default is defensible.
Not yet measured: longer capture windows (unlikely to move a top-1 that is a
precision/feature gap rather than a signal-volume one) and a larger corpus.
