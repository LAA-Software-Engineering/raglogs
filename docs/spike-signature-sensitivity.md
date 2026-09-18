# Spike: signature-equality sensitivity before Phase D (#181)

Time-boxed investigation, **not production code**. It de-risks the structural assumption of the
#177 epic — that the IDENTIFIED / NON_IDENTIFIABLE / UNCERTAIN outcome can rest on categorical
equality of **causal-hypothesis prediction** signatures over **usable** observables — before the
Phase D partitioner is built. Reproduce with:

```bash
python scripts/gen_trace_localization_corpus.py         # -> data/eval-cases/trace-loc (24 cases)
python scripts/spike_signature_sensitivity.py
```

## Method (prototype for the still-open Phases A/C)

- **Hypotheses** `H = {(service, mode) : mode ∈ {error, error_silent, latency}}`. `error_silent`
  models a cause that emits **only** an ERROR *span* locally — no error log, no error-rate spike —
  i.e. the partial-observability case #177 exists for. A fault propagates **up** the caller chain
  (affected = `s` ∪ transitive callers), cause-first.
- **Per-coordinate prediction with strength.** For every `(service, observable)` a hypothesis
  predicts `PRESENT_HARD`, `PRESENT_SOFT`, or `ABSENT_HARD`. The cause's defining signal is hard;
  downstream propagation onto callers is *soft* (it may or may not surface); an error hypothesis
  predicts `lat = ABSENT_HARD` on its affected set (and vice-versa), so a wrong-mode anomaly is a
  real contradiction, not silently "explained" by topology membership.
- **`F_usable` is an explicit per-source availability set** (which observables were measured),
  swept as scenarios. Onset has two sources: error-onset (from ERROR spans) and latency-onset
  (from latency metrics).
- **Consistency = hard-incompatibility only.** A hypothesis survives unless a usable observation
  contradicts a HARD prediction (`PRESENT_HARD` but observed absent, or `ABSENT_HARD` but observed
  present). Crucially there is **no "the cause must be locally anomalous" rule** — a silent cause
  survives on its predicted *downstream* effects.
- **Signature = the observable expectation** (present vs absent) over usable coordinates, plus the
  bucketed observed onset order when an onset source is usable. Two hypotheses share a `~_O` class
  exactly when every usable coordinate that could separate them is unavailable.
- **Outcome from the surviving partition, exactly per #177**: IDENTIFIED = one singleton class;
  NON_IDENTIFIABLE = one class, ≥2 hypotheses; UNCERTAIN = ≥2 classes; NONE = no survivor.
- **Ground truth** (`root_cause` + fault-type→mode) only *scores* whether the truth was retained
  and the resolution is correct — it never builds the label.

## Finding 1 — the structural method never eliminates the true cause, and it produces genuine partial-observability outcomes

Outcome distribution and truth scoring as `F_usable` varies (`err_rate_cut=0.05`, `lat_mult=2.0`,
`onset_tol=5s`):

| `F_usable` | IDENTIFIED | NON_ID | UNCERTAIN | NONE | retained | correct |
|---|---|---|---|---|---|---|
| all | 24 | 0 | 0 | 0 | 24/24 | 24/24 |
| no_onset | 24 | 0 | 0 | 0 | 24/24 | 24/24 |
| metrics_only | 12 | 0 | 12 | 0 | 24/24 | 12/24 |
| spans_only | 0 | 18 | 6 | 0 | 24/24 | 18/24 |
| logs_only | 6 | 10 | 8 | 0 | 24/24 | 16/24 |

The true cause is **retained in every scenario (24/24)** — restricting observability degrades the
outcome from IDENTIFIED toward NON_IDENTIFIABLE / UNCERTAIN, it does **not** silently discard the
cause or return a confident wrong answer. `NON_IDENTIFIABLE` and `UNCERTAIN` are reached for real
(they were structurally impossible in the earlier rounds of this spike), so signature equality is
now actually being tested.

## Finding 2 — availability sets the *resolution type*, and silent causes stay representable (the headline)

The decisive case is `symptom_only`: the root cause is a callee that fails **silently** (only an
ERROR span; no log, flat error-rate), while its caller emits the loud symptom.

| `F_usable` on symptom_only | silent cause retained | outcome | correct |
|---|---|---|---|
| logs_only (the span is **not** collected) | **6/6** | **NON_IDENTIFIABLE 6/6** | 6/6 (truth retained in the class) |
| spans_only (the ERROR span **is** collected) | 6/6 | **IDENTIFIED 6/6** | 6/6 |

This is exactly the #177 contract:

- When the separating measurement (the callee's ERROR span) **is** collected, the silent cause is
  uniquely IDENTIFIED.
- When it is **not** collected, the silent callee and its loud caller predict the *same* usable
  observations, so the honest result is **NON_IDENTIFIABLE** — the true cause is retained inside the
  surviving class, and the engine reports "these are indistinguishable given what was measured"
  rather than confidently returning the loud symptom.

(An earlier round of this spike reported the opposite — "logs-only misidentifies the cause." That
was a modelling bug: it made local anomaly a hard eligibility condition, eliminating the silent
cause by construction. Corrected here: absence of a local signal is not a contradiction.)

## Finding 3 — onset is robust but not load-bearing

Correct-resolution and outcome on `F_usable=all` as the onset policy varies:

| onset_tol_s | jitter_s | IDENTIFIED | correct |
|---|---|---|---|
| 0 (exact) | 0 | 24 | 24/24 |
| 5 | 0 | 24 | 24/24 |
| 5 | 8 | 24 | 24/24 |
| 20 | 0 | 24 | 24/24 |
| order-off (coordinate removed) | 0 | 24 | 24/24 |

The call-graph covering structure separates cause from symptom on its own; onset is identical with
the coordinate removed and stable under ±8s jitter. **Onset is safe as a tolerance-bucketed
tie-breaker but Phase D must not depend on it.** (This coordinate is now genuinely removed when
"order-off", and its discretization is applied to prediction and observation alike — both were bugs
in the previous round.)

## Finding 4 — discretization sensitivity is concentrated in the latency threshold

Correct-resolution count over a factorial `err_rate_cut × lat_mult` grid (`F_usable=all`):

| err_rate_cut \ lat_mult | 1.2× | 1.5× | 2.0× | 3.0× |
|---|---|---|---|---|
| 0.02 | 0 | 20 | 24 | 24 |
| 0.05 | 0 | 20 | 24 | 24 |
| 0.10 | 0 | 20 | 24 | 24 |
| 0.20 | 0 | 20 | 24 | 24 |
| 0.35 | 0 | 20 | 24 | 24 |

Stable (correct ≥ 22/24) in **10/20** cells — every cell with `lat_mult ≥ 2.0`, independent of the
error-rate cutoff. The **error-rate cutoff is not a sensitive knob** (rows identical: error
identification keys on span/log presence). The **latency multiplier is** — degenerate at `1.2×`
(baseline jitter crosses the threshold), stable at `≥ 2×`. The boundary is a latency floor, not a
rectangle in both knobs.

## Recommendation — Phase D proceeds, conditioned on four things

1. **Consistency is hard-incompatibility over an explicit usable-observable set** — never "the cause
   must be locally anomalous." Silent causes must survive on predicted downstream effects; this is
   the mechanism that keeps partial observability representable.
2. **Predict per `(service, observable)` coordinate with hard/soft strength**, and build the
   `~_O` partition from the *observable-expectation* signature over usable coordinates — that is
   what makes NON_IDENTIFIABLE reachable when a distinguisher is unavailable.
3. **Outcome from the surviving partition** (IDENTIFIED / NON_IDENTIFIABLE / UNCERTAIN); ground truth
   only scores. Availability decides the *resolution type*, and the true cause stays retained.
4. **Onset is an optional, tolerance-bucketed, source-gated coordinate** (error-onset from spans,
   latency-onset from metrics), not a dependency; apply a **latency min-delta gate (`≈2×`)** — the
   error-rate cutoff is not sensitive.

**Remaining gap.** This corpus reaches NON_IDENTIFIABLE / UNCERTAIN only by *restricting
availability*; every case still has a single injected fault, so it does not exercise **co-occurring
faults** or **unobserved intermediate nodes** under full observability. Those are the cases where
two hypotheses are indistinguishable even with everything collected — extend the corpus before
Phase E's outcome semantics are trusted on them. Prototype expectation model; no ranker change, no
accuracy claim.
