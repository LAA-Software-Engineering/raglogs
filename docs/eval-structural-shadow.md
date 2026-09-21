# Structural shadow evaluation — candidate recall of the A–E bridge (Phase H2, #186)

Phase H1 measured the *disease* (which failure class dominates the existing pipeline; recorded on
#177). This runs the A–E structural core (#178–#183) on **real** eval telemetry, **offline / in
shadow**, and measures **candidate recall** — can a propagation-aware hypothesis generator, fed
availability-honest observables, produce a small hypothesis set that *retains the true cause*? It
changes **nothing** in the product explain path.

## Scope — what this does and does not measure

This measures **truth retention / candidate recall**, **not structural correctness.** #186's
`struct_ok` additionally requires the partition itself — classes, signatures, `D_missing`, no false
collapse — to be correct under the modeled relation; that needs a faithful symptom-propagation
observation model and per-observable availability ground truth, and is **not measured here.** The full
structural packet (`ClassInfo` with signature + `D_missing`) is preserved on each result so a future
scorer can check it. Candidate recall is not a downgraded metric — for a tool whose contract is
*"produce a small, defensible hypothesis set with evidence"*, retaining the true cause is a real
product metric. It just isn't structural correctness, and this doc does not call it that.

Two **sound** choices this bridge makes (correcting the first cut, per the #202 review):

- **Availability is honest.** A `sig:{service}` observable is emitted only when an incident
  error/latency signal was actually measured. A service with no incident measurement — baseline-only,
  or only unrelated OTLP counters — stays **UNKNOWN**, never a synthesized OBSERVED ABSENT.
- **No unsound hard rule.** An anomalous callee does **not** logically exclude its caller as the root
  (a caller can overload/misuse a callee; multi-fault incidents happen). Dependency direction is *not*
  fed into the hard-contradiction channel — it is left to soft ranking (Phase G). Each hypothesis
  therefore predicts only its own `sig`, so the partition here largely **enumerates candidates**
  rather than doing strong structural elimination. That is exactly why the honest headline is recall.

## Model (`src/eval/structural_shadow.py`)

Per labeled positive case, from its own `metrics.jsonl` + `spans.jsonl` (no DB): `sig:{service}` =
`PRESENT` when anomalous (error present or latency ≥2× baseline, Phase D discretization) else
`ABSENT`, **OBSERVED only when measured**. Candidate hypotheses are the anomalous services plus, when
a service's fault is not already explained by a visible anomalous callee, its callees (so a silent
downstream root is still generated — the coverage gap H1 measured). `partition → resolve`, scored as
`truth_retained`, `unique` (a *structural* IDENTIFIED at the truth, no ranking), `abstained`.

Run it: `python scripts/structural_shadow_eval.py data/eval-cases/trace-loc`.

## Result — synthetic trace corpus (`trace-loc` #170, 24 cases)

| | value |
|---|---|
| **candidate_recall** | **100%** (24/24 — every fault family: callee_fail, caller_fail, latency_only, symptom_only) |
| unique (structural IDENTIFIED at truth) | **0%** — every case is `UNCERTAIN` |
| abstention | 0% |

So the propagation-aware generator **retains the true cause in 100%** of trace-loc cases — including
the `symptom_only` roots the existing pipeline lost as coverage (H1) and the `callee_fail`/`latency_only`
roots it mis-ranked as inference. But the structural core, given only each hypothesis's own `sig`
prediction and **no** unsound elimination, produces `UNCERTAIN` every time: **structural partitioning
alone does not identify a unique cause here** — that resolution is a ranking job (Phase G), which the
epic deliberately keeps separate. What A–E buys on this corpus is *recall + honest non-uniqueness*, not
a confident structural pick.

## Result — real OTel (`otel`, `otel-fresh`, 9 cases each): `no_candidates`

The generator produced **no candidates** on every OTel case: the telemetry is OTLP infra counters
(`process.cpu.time`, `container.memory.percent`, many `service = null`) with `status_code = null`
spans — no per-service error/latency signal for the `sig` adapter to key on. This is **not** an A–E
result at all; it localizes the real-world gap to the **observable adapter** (deriving per-service
signals from messy OTLP + spans), the standing OTel lesson (`project_otel_frozen_external_validation`)
pinned to the observable-derivation layer.

## Read for the checkpoint

- **The A–E bridge achieves 100% truth retention on `trace-loc` under a minimal, propagation-aware
  hypothesis generator.** That is candidate recall, and it is a genuinely useful product signal for a
  "narrow the search space with evidence" contract.
- **Structural *correctness* is not yet measured**, and structural *uniqueness* is 0% here by
  construction (no ranking, no sound elimination rule). Whether raglogs should make strong
  `IDENTIFIED` / `NON_IDENTIFIABLE` claims — vs. hand a small retained set to a human/agent — is a
  product decision, not something this run settles.
- **Real-world value is gated on a faithful OTLP Observable adapter**, not more structural machinery.
  Next honest step: build that adapter, re-run **recall** on OTel + RCAEval RE3, and only invest in a
  faithful symptom-propagation model (to measure real `struct_ok`) if raglogs later needs strong
  structural claims.

Caveats: synthetic clean topology and injected faults; a deliberate minimal v1 hypothesis generator.
