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

### Whole Gen-2 system: ranker + abstention gate, two views

`frozen-eval` evaluates the **whole frozen system** (`--gate`, on by default): the
ranker *and* the abstention gate (#79), whose frozen `τ`/threshold are also
development-only and frozen before this run. It reports two views:

- **Component-level** (each part alone):
  - **gate** — healthy abstention (abstain on negatives) + incident recall (proceed
    on real incidents);
  - **ranker** — top-1 / top-3 + ECE, over **all** incident windows. The ranker is
    scored with the gate **off**, so a good gate can't hide bad ranker cases by
    abstaining on them.
- **End-to-end** (ranker gated by abstention):
  - **coverage** = fraction not abstained;
  - **selective top-1** = RCA accuracy given "proceed";
  - **false-diagnosis rate** on healthy windows (proceeded on a negative).

Also measured: **calibrated-confidence reliability** (ECE) — RE3 warned a constant
base rate beat the learned map, so watch this before users read "78% confidence"
literally — plus **trigger correctness** on deploy-class cases, **behaviour under
confounding**, and **per-fault-class / per-service** breakdowns.

### Corpus requirements for a real portability test

The old `data/eval-cases/otel` corpus is **spent for model selection** (the model
has now been shaped against it). A fresh external run needs a *new* corpus that
actually stresses portability, not a 2-negative smoke set:

- fresh flag-fault incidents (per built-in failure flag);
- **many healthy negatives**, not two — the gate's false-diagnosis rate is only
  meaningful with a real negative sample;
- **varied incident/window durations and baseline lengths** — the gate's `τ`/
  threshold were frozen at ~300 s symmetric windows; production uses a 24 h default
  baseline, and that transfer is unmeasured (see `docs/eval-abstention.md`);
- a few **confounded** cases if feasible (flag flip + unrelated deploy);
- **per-fault / per-service** labels for the breakdowns above.

Treat the resulting numbers as **one-shot external validation**, not another tuning
loop: freeze everything, generate the corpus after, report once.

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

## Fresh-corpus whole-system frozen validation (2026-09-12)

The one-shot external validation the whole Gen-2 effort was building toward: a
**fresh** 21-case corpus (9 flag incidents + **12** healthy negatives, 180 s / 300 s
windows — the earlier corpus is spent for model selection), scoring the **whole
frozen system** — ranker *and* the shipped abstention gate (#159) — with nothing
tuned after capture. Ranker scored on **all** incident windows with the gate off
(`frozen-eval` enforces `ABSTENTION_ENABLED=false` in code); gate scored separately.
`RCA_EXCLUDED_SERVICES=load-generator,flagd,frontend-proxy,image-provider`.

**Component — ranker (all 9 incidents, gate off):**

| metric | RCAEval LOSO | spent corpus (2026-09-10) | **fresh corpus** |
|---|---|---|---|
| root-cause top-1 | 60–77% | 0% | **33.3%** (3/9) |
| root-cause top-3 | — | 22% | **55.6%** (5/9) |
| confidence ECE | 0.07–0.20 | 0.61 | **0.331** |

Per fault class: code 50% (n=2), dependency 50% (n=2), resource 20% (n=5). So the
ranker **does partially generalise** on a fresh, cleanly-generated corpus — top-1
0% → 33%, top-3 22% → 56%, ECE 0.61 → 0.33 vs the first (short-window, distractor-
heavy) run. It degrades from the in-distribution 60–77% but does not collapse;
resource faults (metric-driven) remain the weakest.

**Component — abstention gate, and end-to-end — the gate does NOT transfer:**

| gate | value |  | end-to-end | value |
|---|---|---|---|---|
| incident recall | 100% (9/9) |  | overall coverage | 100% |
| healthy abstention | **0%** (0/12) |  | selective top-1 | 33.3% |
|  |  |  | false-diagnosis (healthy) | **100%** (12/12) |

The gate **never abstains** — it proceeds on all 12 healthy windows (false-diagnosis
100%). Root cause, diagnosed against the live DB: the **metric arm saturates to 1.0
on healthy OTel windows** while the log arm is correctly 0.0 —

```
otel_healthy_1   log_arm=0.0  metric_arm=1.0   score=1.000  (threshold 0.377)
```

OTel exports **raw OTLP metrics including cumulative/monotonic counters**, whose
windowed mean grows over time, so `|mean_incident − mean_baseline| / mean_baseline`
is large on *every* window, healthy or not. RCAEval's metrics were gauge-like, so
the same transform separated cleanly there (78.9% held-out abstention). This is a
**metric-representation mismatch**, not a threshold that needs nudging — exactly the
kind of failure a one-shot external validation exists to expose, and which RCAEval
nested-LOSO completely hid.

**Verdict.**
- **Ranker:** keep, opt-in — it generalises partially (top-1 33% / top-3 56% on
  unseen OTel). Not yet default-on quality, but real signal, much improved once
  infra distractors are excluded and the corpus is clean.
- **Gate:** **do NOT ship default-on.** Before it can, the metric arm must handle
  cumulative counters (rate/delta or metric-type awareness, not raw windowed mean)
  and be re-validated externally; the log arm alone behaves correctly (0.0 on
  healthy). The frozen `logs+metrics` result on RCAEval was real but did not
  transfer because of the metric representation, not the threshold.
- The three-question separation held up as a *diagnostic*: because the ranker was
  scored independently of the gate, the gate's failure did not hide the ranker's
  genuine improvement, and the arm split localised the gate failure to metrics.

Caveats: single run; 180/300 s windows (varied/larger baselines infeasible via live
capture); confounded cases not generated; `kafka` unwinnable (no `service.name`);
9 incidents is small per-fault. Treated as one-shot — no tuning after capture.
