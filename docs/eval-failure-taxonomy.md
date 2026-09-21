# Eval failure taxonomy + corpus scorability (Phase H1, #186)

The #177 epic bet ontology + partitioning (Phases B–E) on the belief that a meaningful share of RCA
misses are **observation-model / non-identifiability** failures, not plain ranking misses. #186 makes
that bet falsifiable — the *decision checkpoint*. This is the cheap, high-information first half: bucket
the **existing** pipeline's failures so the next investment (Phase F coverage vs. Phase G ranking vs.
justifying B–E) is chosen by data, not intuition. It matches the repo's "measure before extend" policy
(#88/#74). No structural inference is wired here; that is Phase H2 (structural shadow eval).

## The taxonomy (`src/eval/taxonomy.py`)

Each **labeled positive** case's existing output — the ranked service candidates `predicted_services`,
the top pick, and the ground-truth service — buckets deterministically:

| Bucket | Condition | Engineering response |
|---|---|---|
| `CORRECT` | top-1 pick == labeled cause | — (not a failure) |
| `DETECTION` | positive case, no explanation produced | fix detection / triggering |
| `COVERAGE` | labeled cause **not** in the *full generated* candidate set | improve causal-object generation (→ Phase F) |
| `INFERENCE` | labeled cause **was** generated but not ranked top-1 | fix ranking/compatibility (→ Phase G) |

`CORRECT` is strict **top-1**. Coverage vs inference is decided against the **full generated candidate
set** (`services_affected` ∪ every ranked candidate, recorded on `ExplainResult.generated_candidates`),
**not** the selected/top-k `predicted_services` — a cause that was generated but dropped during cluster
selection or truncated below top-k is an `INFERENCE` failure, not a generation gap. Getting this wrong
would recommend Phase F when the real work is Phase G. Negative cases and unlabeled positives are out of
scope (`None`).

The report publishes **two named distributions**: `share_of_scored` (bucket / all labeled positives) and
`share_of_failures` (bucket / failures) plus the overall `failure_rate`. #186 asks whether a failure
*class* is "substantial", which is a share **of failures** — for 90 correct / 5 coverage / 5 inference,
coverage is 5% of scored but 50% of failures.

### Why the finer buckets are not guessed here

#177's full taxonomy also has `observability`, `ontology`, `observation-model`, and (correctly
detected) `non-identifiable`. Those are **refinements** of `COVERAGE` / `INFERENCE` that a service-level
pipeline cannot separate on its own:

- a `COVERAGE` miss might be an *ontology* failure (the true cause is an edge/resource/infra event that
  no service candidate can represent) — but service-labeled corpora carry no such ground truth;
- an `INFERENCE` miss might be an *observation-model* failure (the cause was present but its
  expected/forbidden semantics were wrong) or an *observability* failure (the distinguishing signal was
  never usable) — separable only by running the structural machinery (Phase H2).

Reporting them now would be intuition dressed as data — exactly what the checkpoint exists to avoid.

## Corpus × scorable-axis matrix

Not every axis is scorable on every corpus. `existing` = determinable now (this module); `structural` =
needs the Phase H2 shadow eval; `avail-gt` = additionally needs per-observable availability labels.

| Corpus | detection / coverage / inference | structural outcome + finer buckets | `D_missing` correctness | notes |
|---|---|---|---|---|
| **RCAEval RE2/RE3** | ✅ (service labels) | ⛔ needs H2; ontology limited (service-only labels) | ⛔ no availability GT | primary in-corpus RCA |
| **OTel-Demo** | ✅ | ⛔ needs H2 | ⛔ | external transfer — report separately, never tune to (see `project_otel_frozen_external_validation`) |
| **Synthetic trace #170** | ✅ | ✅ (rich causal labels) | ✅ (per-observable availability) | the only corpus that can score `IRREDUCIBLE` / `D_missing`; do **not** overfit to it |
| **Loghub-2.0** | partial (parsing/detection; sparse RCA labels) | ⛔ | ⛔ | log parsing/detection, not localization |

`external_transfer` is always reported separately and never tuned to — the standing OTel lesson.

## The decision checkpoint

`make eval` now reports the taxonomy distribution (counts, shares, and the case ids behind each bucket)
alongside the baseline lift. The checkpoint (recorded on #177 once run over a corpus):

- if **coverage** dominates → Phase F (absence-derived candidates) is justified;
- if **observation-model / non-identifiability** is substantial → the B–E bet has empirical support, and
  Phase H2 is worth building to quantify it;
- if ordinary **inference/ranking** dominates → Phase G (or revisiting the ranker) matters more, and B–E
  should be rescoped rather than extended.

Running it to record the numbers needs a live corpus + DB (integration), so it lands as the follow-up to
this tooling PR.
