# Spike: signature-equality sensitivity before Phase D (#181)

Time-boxed investigation, **not production code**. It de-risks the central structural
assumption of the #177 epic — that the IDENTIFIED / NON_IDENTIFIABLE / UNCERTAIN outcome
can rest on **exact categorical signature equality** over discretized *usable* observables —
before the Phase D partitioner is built. Reproduce with:

```bash
python scripts/gen_trace_localization_corpus.py         # -> data/eval-cases/trace-loc (24 cases)
python scripts/spike_signature_sensitivity.py
```

## The question

Structural identifiability hinges on `C_i ~_O C_j ⇔ S_O(C_i) = S_O(C_j)` over observables
restricted to `F_usable`. Two knobs make that label degenerate:

1. **`|F_usable|` too small** → distinct causes collapse into one NON_IDENTIFIABLE blob;
2. **discretization too fine** → every candidate becomes structurally distinct and
   everything lands in UNCERTAIN.

We want to see both regimes on real-ish data before building on top of the assumption. The
substrate is the 24-case synthetic trace corpus (#170): 3 topologies × 4 fault types × 2
seeds, with ground-truth `root_cause` / `symptom_services` labels, so "did the signature
separate the cause from its loud symptom?" is directly measurable.

## What the corpus telemetry looks like (per fault type)

Signals available on the **cause** vs its **symptom** (the caller that only *reports* the
failure):

| fault type | cause signature | symptom signature | presence-only separable? |
|---|---|---|---|
| `callee_fail` | err_log + err_span + err_rate | err_log + err_span + err_rate | **no** — identical |
| `caller_fail` | err_log + err_span + err_rate (no distinct symptom) | — | n/a |
| `symptom_only` | **err_span only** (trace ERROR, no logs, flat metrics) | err_log + err_span + err_rate | **yes** — distinct |
| `latency_only` | latency spike only | latency spike only | **no** — identical |

The cause always degrades *first* (onset gap); the symptom follows. So on `callee_fail` and
`latency_only` the cause and symptom are **presence-signature-identical** and only **onset
ordering** separates them.

## Finding 1 — `|F_usable|` is low and uneven

Usable presence dimensions (those that actually vary across services in a case):

| fault type | `|F_usable|` |
|---|---|
| callee_fail | 3 |
| caller_fail | 3 |
| symptom_only | 3 |
| **latency_only** | **1** |

`latency_only` exposes the first degenerate regime directly: a **single** usable observable,
so any two latency-inflating services are signature-identical. With presence-only signatures
these cases are unavoidably NON_IDENTIFIABLE.

## Finding 2 — presence-only signatures leave half the corpus non-identifiable

Cause-above-symptom separability by signature, and the outcome split at a sensible cutoff
(`err_rate ≥ +0.05`, `latency ≥ 2× baseline`):

| signature policy | cause>symptom separable | IDENTIFIED | NON_IDENTIFIABLE | UNCERTAIN |
|---|---|---|---|---|
| presence-only | **6 / 18** | 12 | 12 | 0 |
| presence **+ onset** | **18 / 18** | 24 | 0 | 0 |

Per fault type:

| policy | callee_fail | latency_only | symptom_only |
|---|---|---|---|
| presence-only | 0/6 | 0/6 | 6/6 |
| presence + onset | 6/6 | 6/6 | 6/6 |

Presence-only signatures separate the cause **only** on `symptom_only` (where the cause's
signature is genuinely distinct: trace-error *without* logs/error-rate). On `callee_fail` and
`latency_only` the cause and symptom are structurally identical without onset — so the
NON_IDENTIFIABLE label there is *correct*, not a bug: the measurements that would separate
them (relative onset) simply aren't in a presence-only signature.

## Finding 3 — discretization has a wide stable band, one blow-up regime

Partition size `|H/~_O|` (mean over the 24 cases) and outcome split as the cutoffs move,
under the presence+onset policy:

| err_rate cut | latency mult | mean `|H/~_O|` | IDENTIFIED | NON_IDENTIFIABLE | UNCERTAIN |
|---|---|---|---|---|---|
| 0.05 | 2.0× | 1.8 | 24 | 0 | 0 |
| 0.35 | 3.0× | 1.8 | 24 | 0 | 0 |
| 0.20 | 1.5× | 1.9 | 21 | 0 | 3 |
| **0.02** | **1.2×** | **4.2** | 0 | 1 | **23** |

Cutoffs anywhere in `err_rate ∈ [0.05, 0.35]`, `latency ∈ [1.5×, 3×]` are stable. Only an
over-fine cutoff (`0.02` / `1.2×`) triggers the **second** degenerate regime: baseline noise
crosses the threshold, every candidate gets a distinct signature (`|H/~_O|` → 4.2), and
everything collapses to UNCERTAIN.

## Recommendation — Phase D proceeds, with two required tweaks

Signature equality **is** a workable basis for the outcome label on this corpus — it cleanly
separates cause from symptom in **every** case — but only with both of:

1. **Onset ordering must be a first-class signature dimension**, not just presence/absence of
   error/latency. Without it, `callee_fail` + `latency_only` (half the corpus) are
   structurally non-identifiable, because cause and symptom share an identical presence
   signature. This is the single most important input to Phase D's signature definition.
2. **Minimum-support / minimum-delta gating on continuous observables.** Discretize
   `error_rate` and `latency` only against a floor (`err_rate ≥ ~0.05` absolute,
   `latency ≥ ~1.5–2× baseline`); an over-fine cutoff makes baseline noise structurally
   significant and forces the all-UNCERTAIN degeneracy. The stable band is wide, so this is a
   floor, not a tuned knob.

**Phase D proceeds as specified**, with the signature schema extended to carry an onset-rank
dimension and a min-delta gate on the continuous observables. A secondary, honest fallback
follows from Finding 2: where onset is unavailable or untrustworthy, NON_IDENTIFIABLE on
`callee_fail`/`latency_only` is the *correct* structural label, and ranking (onset-based)
should operate **within** that equivalence class rather than being asked to manufacture a
distinction the usable observables don't support — exactly the #177 contract.

## Caveats

Synthetic corpus, prototype observable model (the production `F_usable` and expectation model
are Phase A / Phase C). The onset dimension here is a coarse dense-rank of first-degradation;
Phase D should define its discretization deliberately. No ranker change, no accuracy claim —
this gates Phase D's design only.
