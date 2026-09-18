# Spike: signature-equality sensitivity before Phase D (#181)

Time-boxed investigation, **not production code**. It de-risks the #177 structural assumption — that
the IDENTIFIED / NON_IDENTIFIABLE / UNCERTAIN outcome can rest on categorical equality of
**causal-hypothesis prediction** signatures over **usable** observables — before Phase D (#182)
builds the partitioner. Reproduce with:

```bash
python scripts/gen_trace_localization_corpus.py         # -> data/eval-cases/trace-loc (24 cases)
python scripts/spike_signature_sensitivity.py
```

## Method (prototype for the still-open Phases A/C)

- **Hypotheses** = `service × {error, error_silent, latency}`. `error_silent` = a cause that emits
  **only** an ERROR span locally (no error log, flat error-rate) — the partial-observability case.
  A fault propagates **up** the caller chain (affected = `s` ∪ transitive callers), cause-first.
- **Per-`(service, observable)` prediction with strength** (`PRESENT_HARD` / `PRESENT_SOFT` /
  `ABSENT_HARD`): the cause's defining signal is hard; downstream propagation is soft; wrong-mode
  dims are `ABSENT_HARD`, so a latency anomaly genuinely contradicts an error hypothesis.
- **`F_usable`** = explicit per-source availability, swept. Onset has two sources — error-onset (from
  ERROR spans) and latency-onset (from latency metrics) — and is chosen **per hypothesis mode**.
- **Consistency = hard-incompatibility only**: eliminate iff a usable observation contradicts a HARD
  prediction. **No "cause must be locally anomalous" rule**, so a silent cause survives on predicted
  downstream effects. Observed onset is used *only* here (a hard order check).
- **Signature `S_O(C)` is built purely from the prediction** over usable coordinates: presence
  expectation per `(service, presence-observable)`, plus the predicted categorical onset order
  (hop-rank per affected service) when the mode's onset source is usable. No observed value enters
  the signature, so two hypotheses share a `~_O` class iff their predictions agree on every usable
  coordinate — i.e. only their *unavailable* distinguishers differ.
- **Outcome from the surviving partition, exactly per #177**: IDENTIFIED = one singleton class;
  NON_IDENTIFIABLE = one class, ≥2 hypotheses; UNCERTAIN = ≥2 classes; NONE = no survivor.
- **Scoring uses the exact `(service, mode)` hypothesis.** IDENTIFIED-correct iff the sole survivor
  *is* the true hypothesis. NON_IDENTIFIABLE-correct iff the surviving class contains the true
  hypothesis **and** every co-member differs from it only on **unavailable** coordinates (anti-gaming:
  no co-member a usable coordinate would have split off), with `D_missing` = those unavailable
  distinguishers (non-empty). Ground truth is used only to score — never to build the label.

## Finding 1 — the true hypothesis is never eliminated; observability sets the *resolution granularity*

Outcome distribution and exact-hypothesis scoring as `F_usable` varies (`err_rate_cut=0.05`,
`lat_mult=2.0`, `onset_tol=5s`):

| `F_usable` | IDENTIFIED | NON_ID | UNCERTAIN | retained | correct |
|---|---|---|---|---|---|
| all | 24 | 0 | 0 | 24/24 | 24/24 |
| no_onset | 24 | 0 | 0 | 24/24 | 24/24 |
| metrics_only | 12 | 10 | 2 | 24/24 | 12/24 |
| spans_only | 0 | 24 | 0 | 24/24 | 18/24 |
| logs_only | 6 | 10 | 8 | 24/24 | 16/24 |

The exact true `(service, mode)` hypothesis is **retained in every scenario (24/24)** — restricting
observability degrades the outcome from a unique IDENTIFIED toward NON_IDENTIFIABLE / UNCERTAIN; it
never eliminates the truth or returns a confident wrong answer. Because the signature is built only
from usable coordinates, NON_IDENTIFIABLE and UNCERTAIN are genuinely reachable (they were
structurally impossible in earlier rounds of this spike), so signature equality is actually tested.

## Finding 2 — availability decides *what* is resolvable; silent causes stay representable (headline)

The decisive case is `symptom_only`: a callee that fails **silently** (only an ERROR span; no log,
flat error-rate) under a loud caller. Exact `(service, mode)` scoring shows two *different*
partial-observability regimes:

| `F_usable` on symptom_only | outcome | what is / isn't resolved | truth retained |
|---|---|---|---|
| logs_only (span not collected) | **NON_IDENTIFIABLE 6/6** | the *service* is ambiguous — silent callee ≡ loud caller (`D_missing` ≈ 6 coords) | 6/6 |
| spans_only (span collected; no log/rate) | **NON_IDENTIFIABLE 6/6** | the *service* is found but the **mode** is ambiguous — `error` vs `error_silent` needs `err_log`/`err_rate` | 6/6 |
| all | **IDENTIFIED 6/6** | unique `(service, mode)` | 6/6 |

This is the #177 contract working: the engine reports exactly the resolution the telemetry supports —
a unique cause when every distinguisher was collected, an honest "these are indistinguishable, and
here is what was missing" (`D_missing`) when it wasn't — and it **never** silently returns the loud
symptom. (An earlier round reported "logs-only misidentifies the cause"; that was a modelling bug —
it made local anomaly a hard eligibility condition and eliminated the silent cause. Corrected here.)

## Finding 3 — onset is robust but not load-bearing

On `F_usable=all`, varying the onset policy (now a *predicted* hop-rank coordinate; observed onset
used only for the hard order check):

| onset_tol_s | jitter_s | IDENTIFIED | correct |
|---|---|---|---|
| 0 (exact) | 0 | 24 | 24/24 |
| 5 | 0 | 24 | 24/24 |
| 5 | 8 | 24 | 24/24 |
| 20 | 0 | 24 | 24/24 |
| order-off (coordinate removed) | 0 | 24 | 24/24 |

Presence + covering already separate cause from symptom, so results are identical with the onset
coordinate removed and stable under ±8s jitter. **Onset is safe as a tolerance-bucketed tie-breaker
but Phase D must not depend on it.**

## Finding 4 — discretization sensitivity is concentrated in the latency threshold

Exact-correct count over a factorial `err_rate_cut × lat_mult` grid (`F_usable=all`):

| err_rate_cut \ lat_mult | 1.2× | 1.5× | 2.0× | 3.0× |
|---|---|---|---|---|
| 0.02 | 0 | 19 | 24 | 24 |
| 0.05 | 0 | 19 | 24 | 24 |
| 0.10 | 0 | 19 | 24 | 24 |
| 0.20 | 0 | 19 | 24 | 24 |
| 0.35 | 0 | 19 | 24 | 24 |

Stable (correct ≥ 22/24) in **10/20** cells — every cell with `lat_mult ≥ 2.0`, independent of the
error-rate cutoff. The **error-rate cutoff is not a sensitive knob** (error identification keys on
span/log presence); the **latency multiplier is** — degenerate at `1.2×` (baseline jitter crosses),
stable at `≥ 2×`. A latency floor, not a rectangle in both knobs.

## Recommendation — Phase D proceeds, conditioned on four things

1. **Hard-incompatibility consistency over an explicit usable-observable set** — never "the cause
   must be locally anomalous". Silent causes must survive on predicted downstream effects.
2. **Signatures built purely from per-`(service, observable)` predictions** (with hard/soft
   strength), partitioned over usable coordinates — this is what makes NON_IDENTIFIABLE reachable and
   lets the engine emit `D_missing` (the coordinates that would have separated a class but were not
   collected).
3. **Score / resolve the exact `(service, mode)` object**, with NON_IDENTIFIABLE anti-gaming (no
   co-member a usable coordinate would separate). Observability determines the resolution
   *granularity* — logs alone can leave the *service* ambiguous, spans alone can leave the *mode*
   ambiguous — and the true hypothesis stays retained.
4. **Onset is an optional, tolerance-bucketed, source-gated (mode-aware) prediction coordinate**, not
   a dependency; apply a **latency min-delta gate (`≈2×`)** — the error-rate cutoff is not sensitive.

**Remaining gap.** This corpus reaches NON_IDENTIFIABLE / UNCERTAIN only by *restricting
availability*; every case still has a single injected fault, so it does not exercise **co-occurring
faults** or **unobserved intermediate nodes** (indistinguishable even under full observability).
Extend the corpus with those before Phase E's outcome semantics are trusted on them. Prototype
expectation model; no ranker change, no accuracy claim.
