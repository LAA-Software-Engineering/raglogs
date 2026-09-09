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
- **Negative cases** — load generator running, no fault; `expect_explanation:
  false`. A tool that invents an incident from healthy logs is worse than useless
  on-call.
- **Confounded cases** — flip a flag *and* perform an unrelated deploy in the same
  window; the ground-truth trigger stays the flag flip. Separates causal reasoning
  from "the most recent trigger-looking line wins".

For infrastructure faults flags can't express (network partitions, pod kills, I/O
faults), layer [Chaos Mesh](https://chaos-mesh.org/docs/) (e.g. a `NetworkChaos`
partition between cart and Redis; see Coroot's OTel-Demo + Chaos-Mesh recipe).
Generated cases are small enough to commit as permanent regression fixtures.

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
python scripts/train_rca_ranker.py      --features mm_features_re2re3.jsonl --out models/rca_ranker.json
python scripts/train_rca_calibrator.py  --features mm_features_re2re3.jsonl --out models/rca_calibrator.json
RCA_RANKER_MODEL_PATH=models/rca_ranker.json \
RCA_CALIBRATOR_MODEL_PATH=models/rca_calibrator.json \
  raglogs eval --cases data/eval-cases/otel --json eval_otel.json
```

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
