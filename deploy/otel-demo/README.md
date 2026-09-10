# Standing up the OTel-Demo corpus for frozen external validation (#79)

The one-command goal: **start the demo → run the driver → run `frozen-eval`.** This
directory has the demo-side wiring; the driver and protocol live in the repo
(`scripts/eval/otel_corpus.py`, `docs/eval-otel-demo.md`).

## 0. Validate wiring with no cluster (do this first)

```bash
python scripts/eval/otel_corpus.py --dry-run --out-dir /tmp/otel-dry
```

Emits every flag case + negatives with synthetic capture — confirms the loop,
scenario table, and case format before you spend time on the cluster.

## Path A — docker-compose (flag + negative + deploy cases)

1. Get the demo and wire the Collector to export OTLP-JSON:
   ```bash
   git clone https://github.com/open-telemetry/opentelemetry-demo.git
   cd opentelemetry-demo
   # merge the file exporters into the collector config:
   cp <raglogs>/deploy/otel-demo/otelcol-config-extras.yml src/otel-collector/otelcol-config-extras.yml
   # mount the export dir into the collector:
   cp <raglogs>/deploy/otel-demo/compose.override.yml docker-compose.override.yml
   mkdir -p export
   docker compose up -d
   ```
   flagd's feature API is served at `http://localhost:8080` (`/feature/api/read|write`).

2. **Confirm service names match** (the one gotcha): flip a flag in the UI at
   `http://localhost:8080/feature`, watch `export/logs.json`, and check the
   `service.name` matches the target in `src/eval/otel_demo.py::FLAG_SCENARIOS`
   (it has drifted across demo versions). Fix the table if not.

3. Generate the corpus (serial + slow — each case waits `baseline + post`; lower
   them for a smoke run):
   ```bash
   cd <raglogs>
   python scripts/eval/otel_corpus.py \
       --flagd-url http://localhost:8080 \
       --otlp-dir /abs/path/to/opentelemetry-demo/export \
       --out-dir data/eval-cases/otel
   ```

## Path B — Kubernetes (adds Chaos Mesh infra faults)

```bash
kind create cluster
helm install otel-demo open-telemetry/opentelemetry-demo      # add the file exporters via Helm values (same yaml)
helm install chaos-mesh chaos-mesh/chaos-mesh -n chaos-mesh --create-namespace
kubectl port-forward svc/otel-demo-frontendproxy 8080:8080
```

Then generate the infra-fault cases (Chaos-Mesh manifests are in
`deploy/otel-demo/chaos/`, one per `CHAOS_SCENARIOS` entry):

```bash
# dry-run first (no cluster): logs the apply/delete + emits the 4 case dirs
python scripts/eval/otel_corpus.py --chaos --dry-run --out-dir /tmp/otel-chaos-dry

# for real (needs kubectl context on the cluster + the Collector export at --otlp-dir)
python scripts/eval/otel_corpus.py --chaos \
    --otlp-dir /path/to/collector/export --out-dir data/eval-cases/otel
```

The driver `kubectl apply`s each manifest, waits the incident window, captures, and
`kubectl delete`s it. **Before a real run, edit the manifests** in
`deploy/otel-demo/chaos/` so the namespace + label selectors (and `IOChaos`
`volumePath`) match your deployment — see the comments in each file. On k8s you
must also make the Collector's OTLP-JSON export reachable at `--otlp-dir` (a
hostPath/PV mount, or periodic `kubectl cp`). Coroot's "OTel Demo + Chaos Mesh"
post is the reference recipe.

## The frozen run (the milestone)

No retraining, no threshold/feature/calibration fitting — run the committed
artifacts against the never-seen corpus:

```bash
raglogs frozen-eval data/eval-cases/otel \
    --ranker models/rca_ranker.json --calibrator models/rca_calibrator.json
```

A large drop from the RCAEval LOSO (RE3 60% / RE2 77%) is not a failure — it's the
headline result: a good RCAEval-generalising model vs a general one. See
`docs/eval-otel-demo.md` for what to measure (top-1/3, abstention on negatives,
calibrated-confidence ECE, trigger correctness, confounding).
