# raglogs eval harness

Measures the one thing the product exists to do — produce a correct,
evidence-backed explanation — and reports raglogs' **lift over a trivial
baseline**, not just its absolute score. Run it with:

```bash
make eval          # ingest each case, run raglogs + baseline, print a table
make eval-data     # download external corpora (they are too large to commit)
```

`make eval` needs a database (it ingests each case and runs the real explain
pipeline). It writes `eval_results.json` for diffing runs over time.

## Case format

One directory per case, source-agnostic:

```
tests/eval/cases/<case-id>/
  case.yaml           # ground truth
  logs.jsonl          # optional; the case's logs
```

`case.yaml`:

```yaml
id: deploy-regression-001
window:
  start: "2026-03-16T15:00:00+00:00"
  end:   "2026-03-16T16:10:00+00:00"
# Where the logs live. A path/dir/glob relative to the repo root, or a list of
# them. Omit to use a logs.jsonl in the case directory.
logs: sample_data/sample_incident
root_cause:                      # required unless expect_explanation is false
  service: billing-worker        # required
  fingerprint_hint: "signature verification failed"   # optional substring
trigger:
  timestamp: "2026-03-16T15:15:30+00:00"   # the moment the change landed
  type: deploy                             # deploy|config|dependency|resource|code|none
expect_explanation: true         # false for negative ("nothing happened") cases
notes: "..."
```

## Metrics (reported per run, plus aggregate)

- **Root-cause service accuracy** — does `primary_cluster.services` contain the
  labeled service?
- **Trigger accuracy** — is the top trigger candidate within tolerance
  (default 5 min) of the labeled trigger? Reported alongside the *any-trigger*
  rate (did we return one at all?).
- **Negative-case precision** — on `expect_explanation: false` cases, do we
  correctly return insufficient-evidence instead of inventing a story?
- **Confidence calibration** — cases bucketed by reported confidence, with the
  actual accuracy per bucket. `high` should be right far more often than
  `medium`; if not, the scoring in `confidence.py` is decorative.

## Baseline arm

A deliberately trivial arm — **the most frequent error/fatal cluster in the
window, no trigger, no narrative** — is scored on every run. The report shows
raglogs' lift over it. If the lift is ~0, that is the most important thing the
team could learn, and the harness makes it impossible to avoid.

## Seed cases

- `001-sample-incident` — the committed `sample_data/` fixture the product was
  fitted to. A **canary**: expect near-100%; a high score proves the wiring,
  not the quality.
- `002-quiet-healthy`, `003-steady-state` — negative cases where the correct
  answer is "nothing significant happened".

## External corpora

Large labeled corpora are downloaded (never committed) and converted into the
same case format under gitignored `data/eval-cases/`.

### RCAEval RE2/RE3 (`make eval-data`)

[RCAEval](https://github.com/phamquiluan/RCAEval) (MIT) ships 360 labeled
failure cases whose `inject_time.txt` is exactly the trigger label. `make
eval-data` downloads it (~3.4 GB) and converts each case — deriving the
root-cause service and fault type from the directory name and the trigger from
`inject_time.txt` — into `data/eval-cases/rcaeval/{re2,re3}/`. Then:

```bash
raglogs eval --cases data/eval-cases/rcaeval/re2 --json eval_re2.json
raglogs eval --cases data/eval-cases/rcaeval/re3 --json eval_re3.json
```

Score **RE2 and RE3 separately**: RE2 faults (CPU/memory/disk/network stress)
don't announce themselves in the logs, so raglogs' pattern-based trigger
detection is expected to score near-zero there — that gap is the finding, and it
motivates the trigger-redesign work (#82). RE3 (code-level faults, visible as
stack traces) is where raglogs should be competitive.

OTel-demo (#79) and Loghub-2.0 (#80) will plug in here the same way.
