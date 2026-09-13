# Trace-localization benchmark (#118 / #79)

A small, deliberately-adversarial **synthetic** corpus for the question RCAEval cannot
answer: can trace topology + ERROR/latency **onset** tell an upstream *cause* from a
downstream propagated *symptom*? RCAEval is 100% oracle (the truth is always already a
candidate) and carries essentially no ERROR-status spans, so tweaking trace features
against it is cargo-cult benchmarking (see `docs/eval-trace-propagation.md`). This corpus
exists to make the causal-localization question measurable.

## What it is

`scripts/gen_trace_localization_corpus.py` generates 24 cases (3 microservice topologies ×
4 fault types × 2 seeded variants), deterministically, to a gitignored
`data/eval-cases/trace-loc/`. Each case is a call graph with an injected fault; the
telemetry (logs + spans-with-status + metrics) is constructed so the **signal that betrays
the cause is controlled per fault type**. Ground-truth **causal labels** live in each
`case.yaml`'s `trace_localization` block (`src/eval/trace_localization.py`): `root_cause`,
`first_failing`, `propagation_path`, `symptom_services`, `edges`, `fault_type`.

### Fault types (the case variety)

| fault type | cause is visible in… | adversarial? |
|---|---|---|
| `callee_fail`  | error logs + ERROR spans + error-rate metric (full signal) | control (solvable) |
| `symptom_only` | **only** trace ERROR status — no logs, flat error-rate, flat span-rate; the *caller* has the loud logs + metric spike | **yes** |
| `latency_only` | **only** span/latency inflation — no ERROR status, no logs, no error-rate | **yes** |
| `caller_fail`  | full signal, and the cause is the loudest service | trivial control |

The cause degrades at inject; the symptom (its caller) `ONSET_GAP_S` later — so **onset
ordering** is the causal signal that separates them.

## Scoring

`src/eval/trace_localization.py` scores, besides top-1/top-3, the headline
**cause-above-symptom**: of the cases with a distinct loud symptom, how often the method
ranks the true cause above *every* symptom service. The trivial "loudest-error" baseline is
built to fail this, so a method that scores well is doing *causal* work, not correlating
telemetry volume. Run it with `scripts/eval_trace_localization.py` (needs a Postgres/pgvector
DB; ingests each case under its own run-isolated scope, exactly like `raglogs frozen-eval`).

## Baseline result (2026-09-13, committed ranker + reranker)

| method | top-1 | top-3 | cause-above-symptom |
|---|---|---|---|
| baseline (loudest error) | 33.3% | 50.0% | 11.1% |
| ranker | 70.8% | 75.0% | 61.1% |
| + propagation rerank | 70.8% | **100.0%** | 61.1% |

Per fault type (cause-above-symptom): `callee_fail` ranker 100%; `latency_only` 83%;
**`symptom_only` 0%** for every method.

### What the benchmark reveals (the next frontier)

1. **`symptom_only` is unsolved.** When the cause is visible *only* in trace ERROR status
   (no logs, no error-rate), the learned ranker scores it ~0 (its log/rate/metric features
   are all flat) and ranks the loud symptom first. The propagation reranker **cannot rescue
   it**: the reranker is *multiplicative* (`new = base·(1+blend·tanh(adj))`), so a candidate
   the ranker scored ~0 stays ~0 no matter how strong the propagation evidence. The reranker
   *does* help where the cause already has a non-trivial base score — it lifts **top-3 from
   75% to 100%** — but it can't manufacture rank-1 from zero.
2. **The next algorithmic step is candidate *scoring*, not reranking**: a trace-ERROR-status
   **ranker feature** (so a status-only cause gets a non-zero base score) and/or an additive
   / rank-based reranking that can lift a low-scored candidate. This corpus is the substrate
   to develop and validate that — and, unlike RCAEval, it can actually measure it.
3. **`latency_only`** is largely handled by the ranker's p95-duration feature (83%); the
   reranker adds nothing there (it deliberately uses only ERROR-status evidence, latency
   having been rejected on RCAEval — `docs/eval-trace-propagation.md`).

The corpus is synthetic, so a win here proves the *mechanism* is causally correct, not that
it transfers; external validation on captured OTel/Chaos traces remains the final step.

## The eval discipline (sealed DEV / TEST split)

The synthetic `symptom_only` case has done its job: it exposed the mechanism weakness (a
trace-status-only cause is a candidate but the ranker prefers the loud symptom, and the
multiplicative reranker can't lift it). **From here the synthetic corpus is a
mechanism/unit benchmark only** — "does the code obey the causal rule?" — never an
"RCA accuracy improved" claim. Continuing to change the reranker until this synthetic case
passes would be training against our own exam.

Real progress is measured on **captured** OTel/Chaos traces, under a sealed split
(`src/eval/sealed_split.py`, `scripts/seal_corpus_split.py`). The rigorous cycle:

1. **Capture** a real OTel/Chaos trace corpus (ERROR-status spans + parent/child + labels)
   — `scripts/eval/otel_corpus.py`, `deploy/otel-demo/`.
2. **Seal, before touching the algorithm**: `seal_corpus_split.py` holds out **whole fault
   families / services** (not random cases) into a TEST split and writes `split.yaml` with a
   fingerprint. The dev eval scores **DEV only**; TEST is refused unless explicitly unsealed.
   Holding out by family/service asks whether the trace logic *transfers*, not whether it
   memorises a particular injected failure.
3. **Develop** the trace-status reranker/feature with: **RCAEval** = no-regression guard,
   **synthetic** = mechanism test, **real DEV** = model/feature selection.
4. **Freeze.**
5. **real TEST = one-shot** (`eval_trace_localization.py --sealed-test`). A win here is the
   first real evidence trace-status localization works; a failure kills the arc with
   confidence, like the shelved abstention gate.

`eval_trace_localization.py` respects `split.yaml` automatically — DEV by default, TEST only
under `--sealed-test`.

## Real-OTel finding → the symptom-anchor pivot

A first real capture (OTel Demo, flag faults, ERROR-status traces) **falsified the specific
hypothesis "ERROR-status span ⇒ likely root cause."** Where the error status landed vs the
injected cause, across the captured cases:

| case | cause | ERROR-status spans on |
|---|---|---|
| paymentFailure | payment | payment **and** checkout, frontend (callers) |
| cartFailure | cart | cart **and** checkout |
| paymentUnreachable | payment | frontend, checkout — **payment: none** |
| recommendationCacheFailure | recommendation | frontend, frontend-proxy — **recommendation: none** |
| productCatalogFailure | product-catalog | **none anywhere** |

On real traces the failing service usually does **not** mark its own span ERROR — its
*callers*, whose RPC failed or timed out, do. So an ERROR-status span is a **symptom anchor,
not cause evidence**; ranking error-status services directly would promote the caller/symptom.

**The pivot** (`propagation.failed_edge_dependencies`): an ERROR span implicates the specific
**dependency it was calling** — the service of its child span (the callee), or its own service
when it is an erroring leaf (paymentFailure). A dependency implicated by many error spans is
the shared culprit. That score feeds the reranker's **additive** dependency term
(`propagation_scores(..., dependency_boost=...)`), which — unlike the multiplicative onset
term — can lift a *silent* dependency the base ranker scored ~0 (the `symptom_only`
multiplicative-ceiling finding above). Known gap, left for the one-shot to expose rather than
patched around: a truly *unreachable* callee emits no span at all, so it can't be implicated by
a child edge (paymentUnreachable) — that needs a callee span-rate-drop signal this doesn't use.

**Discipline (strict):** the first real cases are **spent hypothesis-formation evidence** — the
mechanism is formulated from them, never tuned against or scored on them. Validation is:
**RCAEval LOSO no-regression** (the log-onset path is unchanged — +8.9pp RE3 system-out
preserved exactly; the dependency term is inert there, RCAEval carrying no error-status) →
**synthetic topology unit tests** for the mechanism (`tests/unit/test_rca_propagation.py`:
`TestFailedEdgeDependencies`, `TestDependencyBoostLiftsSilentCause`) → **freeze** → **one-shot
on NEW captured cases**. If it fails the one-shot, the trace-localization arc is killed with
confidence, like the shelved abstention gate. (The synthetic `symptom_only` fault type predates
this finding — it marks the *cause* with error-status; realigning it to the real
caller-errors/callee-silent pattern is a follow-up, kept separate so the mechanism unit tests
stay the source of truth in the meantime.)
