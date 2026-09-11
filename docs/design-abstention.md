# Abstention gate design (#79 / #118 gen-2)

## Problem (today)

The frozen OTel-Demo run (#79) abstained on **0/2** healthy windows — raglogs
manufactured an incident from healthy background traffic. The cause is in
`src/core/explain/summarizer.py`: raglogs returns *insufficient evidence* only
when a window has **no clusters at all**. Any window with baseline-level errors
(a load generator, routine retries, a noisy `WARN`) gets a confident narrative.
"Confidently wrong on a healthy window" is worse on-call than "I don't know".

The abstention spike (`scripts/spike_abstention_rca.py`,
`docs/spike-abstention-rca.md`) showed a **window-anomaly score** — "is anything
actually elevated vs the baseline, in any modality?" — separates real incidents
from healthy pre-injection windows at **RE3 AUC 0.927 / RE2 AUC 0.917**, across all
six system×suite combos (0.88–0.98). The signal is real. This doc specifies how to
turn it into a shippable gate **without** the trap the spike disclosed.

### Why the spike's raw score cannot ship as-is

The spike's raw score is dominated by novel-error-rate blow-up: a service with
errors in the window and ≈0 in the baseline produces a ratio in the `1e8` range, so
the RE3 threshold came out at `~4.7e7` while RE2's (metric/trace-driven) came out at
`3.65`. That raw quantity **scales with traffic volume, logging verbosity, and
incident duration** — so a single numeric threshold would mean different things in
different installations, making abstention a per-installation tuning exercise. That
defeats the goal: a *defensible default*. The fix is to give the score **portable
semantics** before any threshold is chosen.

## Design: normalized, bounded, per-modality → fused

### Contract

```
each available modality  → anomaly strength in [0, 1]   (fixed transform)
a missing modality       → ABSENT, not 0 evidence       (tracked separately)
fuse available modalities → window anomaly score in [0, 1]
score < threshold        → insufficient_evidence (abstain); else run RCA
```

The score answers one question: **"is there strong evidence of an incident in any
available modality?"** — deliberately orthogonal to the root-cause ranker's "which
service?" (see *Where it plugs in*).

### Per-modality normalization (the important part)

Each modality's raw anomaly quantity `x ≥ 0` (relative/anomaly quantities, not raw
counts, wherever possible) is mapped to `[0, 1]` by a **fixed saturating transform**:

```
s = 1 − exp(−x / τ)            # preferred: smooth, saturating, one scale τ
```

(equivalently a clipped log transform `s = clip(log(1+x) / log(1+x_cap), 0, 1)`).

- **Log arm** is the one that must be tamed: `x_log` is a relative error-anomaly
  (e.g. incident-vs-baseline error-rate elevation), passed through `1 − exp(−x/τ_log)`
  so verbosity/volume can't push it off-scale. `τ_log` is a **fixed scale chosen on
  the development corpus, then frozen** — it is part of the transform, not a
  per-deployment knob.
- **Trace arm** (`tr_rate`/`tr_dur` ratios) and **metric arm** (`met_anom` change
  ratio) are already bounded-ish; put them through the *same* family of saturating
  transforms with their own fixed `τ_trace` / `τ_metric` so all three arms live on
  an equivalent `[0, 1]` scale before fusion.

### Fusion

```
window_anomaly = max(s_log, s_trace, s_metric)   # over AVAILABLE modalities only
```

`max` encodes "strong evidence in *any* modality is enough to attempt RCA" — which
matches the spike (a fault that surfaces only in metrics, RE2-style, still trips the
gate). A missing modality is dropped from the `max`, **not** fed in as `0`: absence
of traces must not read as "traces say all-clear". Availability is tracked with the
existing `has_logs` / `has_traces` / `has_metrics` flags.

### Two hard rules (portability)

1. **The transform is fixed; only the threshold is learned.** `τ_*` and the fusion
   are frozen constants. The single number selected from development data is the
   **abstention threshold** on the `[0, 1]` fused score.
2. **No inference-time corpus normalization.** Never normalize by corpus min/max or
   percentiles at inference — that would make `0.25` mean different things in
   different installations, re-introducing the very problem we're removing. The
   `[0, 1]` mapping comes only from the fixed transforms.

## Where it plugs in

- A new pure function in `src/core/rca/` (e.g. `abstention.py`): takes the
  per-modality anomaly inputs already available in the pipeline
  (`compute_features` gives trace/metric; the log arm from an incident-vs-baseline
  error-rate elevation) → returns `window_anomaly ∈ [0,1]` + which modalities were
  present. Pure and unit-testable, mirroring `features.py`.
- `summarizer.explain_window` consults it **before** committing to a narrative:
  when the gate says abstain, return the existing `render_insufficient_evidence`
  result (the machinery already exists) instead of a fabricated primary cluster.
- **Orthogonal to the ranker.** Abstention = "is there an incident at all"; the
  ranker = "which service". They compose: abstain → skip ranking / return
  insufficient evidence. It also does **not** reuse the cluster `change_ratio`
  machinery as its primary signal — that entangles "should I diagnose at all?" with
  the old count/change/onset selection that behaves differently across fault
  families. The gate is its own stable signal.

## Posture (recommended)

**Opt-in, default OFF** — a new `abstention_enabled: bool = False` setting, exactly
like the ranker/calibrator artifact paths. Rationale:

- Zero behaviour change by default ⇒ **eval delta none** until enabled; no risk of
  silently suppressing a real incident on an existing user's logs.
- `τ_*` and the default threshold ship in-repo (from development-corpus selection),
  documented; an operator flips the gate on when they accept that calibration.
- A later PR can promote it to default-on once it has held up on an untouched
  external corpus (the discipline below).

## Calibration & eval plan

- Select `τ_log` / `τ_trace` / `τ_metric` **and** the abstention threshold on
  **RCAEval development data only** (incident windows vs pre-injection healthy
  windows, as in the spike), then **freeze all of them** before the next untouched
  external run — otherwise the external corpus quietly becomes training data (same
  discipline as the frozen ranker/calibrator and #83).
- Report, at the chosen threshold: incident-explain rate (recall of real
  incidents — the number that must stay high; suppressing a real incident is the
  costly error) and healthy-abstention rate, per suite and per system, plus the
  threshold-free AUC (already RE3 0.927 / RE2 0.917).
- Validate the *frozen* gate on a fresh external corpus (a genuinely healthy
  production-style window, not a pre-injection proxy) before considering default-on.

## Open questions for review

1. **Fusion:** `max` is the natural "any modality" rule; is a softer combiner
   (e.g. noisy-OR) ever wanted, or does `max` + a good threshold suffice? (Start
   with `max`.)
2. **Log-arm input `x_log`:** incident-vs-baseline error-rate elevation is the
   spike's signal; should novelty (a cluster absent from baseline) feed it too, or
   does that re-enter the `change_ratio` entanglement we're avoiding?
3. **Asymmetric cost:** bias the threshold toward *explaining* (never suppress a
   real incident) even at the cost of more false alarms — i.e. pick the operating
   point from the recall side, not balanced accuracy?

_Part of #79 / #118 / #74. No `src/core` change in this doc — design only, pending
review of posture and the score contract above._
