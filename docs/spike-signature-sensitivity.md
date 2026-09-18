# Spike: signature-equality sensitivity before Phase D (#181)

Time-boxed investigation, **not production code**. It de-risks the structural assumption of the
#177 epic — that the IDENTIFIED / NON_IDENTIFIABLE / UNCERTAIN outcome can rest on exact
categorical equality of **causal-hypothesis prediction** signatures over **usable** observables —
before the Phase D partitioner is built. Reproduce with:

```bash
python scripts/gen_trace_localization_corpus.py         # -> data/eval-cases/trace-loc (24 cases)
python scripts/spike_signature_sensitivity.py
```

> **History.** The first version of this spike (PR #195, round 1) was wrong in a way the review
> caught: it partitioned *services by the telemetry they emitted* and consulted ground truth to
> *build* the outcome label. That measures "did services emit different local signals," not "do
> causal hypotheses predict the same observable state." This version replaces both the
> representation and the outcome semantics; its conclusions **supersede** round 1's (in particular
> round 1's "onset ordering is essential" was an artifact of the wrong model — see Finding 3).

## Method

The structural inference Phase D/E require, with a **prototype** expectation model standing in for
the still-open Phases A (observables/availability) and C (observation expectations):

1. **Hypotheses** `H = {(service, mode) : mode ∈ {error, latency}}` — "service *s* is the root
   cause, failing in mode *m*."
2. **Forward prediction** `S(h)`: a fault at a callee propagates **up** the caller chain
   (a caller of a failing/slow dependency sees errors/latency), cause-first. Affected set =
   `{s} ∪ transitive-callers(s)`; each affected service is predicted to show the mode signal, with
   a predicted onset rank = hop distance from the cause. Deliberately simple and **ground-truth-free**.
3. **`F_usable`** is an *explicit availability set* (which observable types were measured), swept as
   scenarios — not derived from whether service values happened to vary.
4. **Consistency (covering / hard-incompatibility, Phase C spirit):** a hypothesis survives iff it
   can **explain every observed anomaly** over `F_usable` and is itself anomalous — i.e. the observed
   anomalous services (in the hypothesis's mode) are a subset of its affected set, the cause is
   itself anomalous, and no other-mode anomaly is left uncovered. This is the causal content the
   exact-match signature lacked: a downstream symptom **cannot** explain its own callee's anomaly, so
   it is ruled out as a cause by topology alone.
5. **Outcome (exactly #177), from the surviving partition only:** IDENTIFIED = one surviving class
   that is a singleton; NON_IDENTIFIABLE = one class, ≥2 hypotheses; UNCERTAIN = ≥2 classes;
   NONE = no survivor (a prototype-model-fidelity failure, reported, not hidden).
6. **Ground truth** (`root_cause` + fault-type→mode) is used **only** to score, per case, whether the
   true hypothesis was *retained* and the resolution is *correct* — never to build the label.

## Finding 1 — availability of usable observables is decisive (the epic's premise, shown structurally)

Outcome distribution and truth scoring as `F_usable` varies (`err_rate_cut=0.05`, `lat_mult=2.0`,
`onset_tol=5s`):

| `F_usable` | IDENTIFIED | NON_ID | UNCERTAIN | NONE | retained | correct |
|---|---|---|---|---|---|---|
| all (logs+spans+metrics+onset) | 24 | 0 | 0 | 0 | **24/24** | **24/24** |
| no_onset | 24 | 0 | 0 | 0 | 24/24 | 24/24 |
| metrics_only (err_rate, lat, onset) | 24 | 0 | 0 | 0 | 18/24 | **18/24** |
| spans_only (err_span, onset) | 18 | 0 | 0 | 6 | 18/24 | 18/24 |
| logs_only | 18 | 0 | 0 | 6 | 12/24 | **12/24** |

The **same fault** resolves correctly or incorrectly purely as a function of which observables are
usable — exactly the #177 contract that the outcome must be conditioned on usable observations:

- **logs-only** structurally *misidentifies* the `symptom_only` faults (the cause is silent in logs,
  so covering picks the loud caller — the symptom) **and** is blind to `latency_only` (no error
  anomaly to explain → NONE): 12/24 correct.
- **spans** resolve `symptom_only` (the cause carries an ERROR span even with no logs); they are
  blind to `latency_only` (NONE) — 18/24.
- **metrics** miss the silent-callee `symptom_only` faults (flat error-rate on the cause) — 18/24.
- **full telemetry** identifies all 24.

## Finding 2 — topology + covering does the identification; the corpus barely exercises the non-singleton branches

Under covering consistency the cause is the *deepest anomalous node whose upward closure explains
every observed anomaly*, so a single-injected-fault case almost always yields a unique survivor →
IDENTIFIED. NON_IDENTIFIABLE and UNCERTAIN essentially do not occur on this corpus. **That is a
limitation of the corpus, not evidence the outcome is robust:** every case has exactly one injected
fault and a unique deepest anomalous service. Phase D's NON_IDENTIFIABLE / UNCERTAIN branches —
the whole point of the epic — are **not** exercised here and must be validated on a richer corpus
(co-occurring faults, and unobserved intermediate nodes that make two causes genuinely
indistinguishable).

## Finding 3 — onset ordering is *not* load-bearing (retracting round 1)

Correct-resolution rate on `F_usable=all` as the onset policy varies:

| onset_tol_s | jitter_s | IDENTIFIED | correct |
|---|---|---|---|
| 0 (exact) | 0 | 24 | 24/24 |
| 5 | 0 | 24 | 24/24 |
| 5 | 3 | 24 | 24/24 |
| 5 | 8 | 24 | 24/24 |
| 20 | 0 | 24 | 24/24 |
| ∞ (onset order ignored) | 0 | 24 | 24/24 |

With a proper covering model the call-graph topology already separates cause from symptom, so onset
is **robust but redundant** here — identical results with onset disabled and under ±8s jitter.
Round 1 claimed onset was essential (6/18 → 18/18); that was an artifact of the telemetry-vector
representation, where cause and symptom had identical local vectors and only the (tautological,
generator-guaranteed, ms-unique) onset rank separated them. **Onset is safe to include as a
tie-breaking refinement, but Phase D must not depend on it**, and if used it needs a declared
tolerance bucket (not raw-timestamp ranks).

## Finding 4 — discretization sensitivity is concentrated in the latency threshold

IDENTIFIED count over a factorial `err_rate_cut × lat_mult` grid (`F_usable=all`, onset_tol=5s):

| err_rate_cut \ lat_mult | 1.2× | 1.5× | 2.0× | 3.0× |
|---|---|---|---|---|
| 0.02 | 0 | 21 | 24 | 24 |
| 0.05 | 0 | 21 | 24 | 24 |
| 0.10 | 0 | 21 | 24 | 24 |
| 0.20 | 0 | 21 | 24 | 24 |
| 0.35 | 0 | 21 | 24 | 24 |

Stability criterion (IDENTIFIED ≥ 22/24): met in **10/20** cells — every cell with `lat_mult ≥ 2.0`,
independent of the error-rate cutoff. Two clear facts, now *measured* rather than extrapolated from
four paired points:

- **The error-rate cutoff is not a sensitive knob** here — the rows are identical, because error
  identification keys on span/log *presence*, not a thresholded rate.
- **The latency multiplier is the sensitive knob**: `lat_mult = 1.2×` is degenerate (baseline jitter
  crosses the threshold → the cause's latency anomaly is not cleanly separable → 0 identified);
  `1.5×` is borderline (21/24); `≥ 2.0×` is stable. The boundary is at `lat_mult ≈ 2×`, not a
  rectangle in both knobs.

## Recommendation — Phase D proceeds, conditioned on three things

1. **Condition the outcome on an explicit usable-observable (availability) set.** The same fault is
   IDENTIFIED-correct or IDENTIFIED-wrong purely by which observables are usable; logs-only
   telemetry structurally points at the symptom on silent-callee faults. Availability metadata is a
   first-class input to Phase D, not an afterthought (Phase A).
2. **The load-bearing mechanism is covering/topology consistency** (a cause must explain every
   observed anomaly), *not* signature equality over local telemetry and *not* onset ordering. Phase D
   should build the partition from hypothesis-prediction signatures under covering; onset is an
   optional, tolerance-bucketed tie-breaker.
3. **Min-delta gate on latency only** (`lat_mult ≈ 2×`); the error-rate cutoff is not sensitive.

**Caveat / gating remains open on one axis.** This corpus does not exercise NON_IDENTIFIABLE or
UNCERTAIN at all, so Phase D's handling of *genuinely indistinguishable* hypotheses is **not**
validated here. Before or alongside Phase D, extend the corpus with co-occurring faults and
unobserved intermediate nodes so those branches can be measured — otherwise Phase E's outcome
semantics ship untested on the very cases they exist for. Synthetic corpus + prototype expectation
model (production `F_usable`/expectations are Phases A/C); no ranker change, no accuracy claim.
