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

**Stabilize first, saturate second.** Bounding a quantity is *not* the same as
making it portable. `s = 1 − exp(−x/τ)` stops a `1e8` ratio from exploding
numerically, but if `x_log` is enormous *because the baseline error rate is nearly
zero*, saturation just maps `4.7e7 → 0.999999…` — the instability is hidden, not
removed, and the score still isn't comparable across workloads. So each modality's
input `x` must itself be a **stabilized, dimensionless-ish effect size** *before*
the saturating transform maps it to `[0, 1]`:

```
x_log = log(1 + incident_error_rate) − log(1 + baseline_error_rate)   # stabilized effect size
s_log = 1 − exp(−x_log / τ_log)                                       # then saturate to [0,1]
```

- **Log arm** — use the stabilized log-rate difference (or another dimensionless,
  bounded-ish anomaly) as `x_log`, **not** the raw incident/baseline ratio. The
  `log(1+·)` on each rate keeps a near-zero baseline from producing a pathological
  input in the first place; `1 − exp(−x_log/τ_log)` then saturates it.
- **Trace arm** (`tr_rate`/`tr_dur`) and **metric arm** (`met_anom`) get the *same*
  treatment: form a stabilized effect size, then the same saturating family with
  their own scales, so all three arms live on an equivalent `[0, 1]` scale before
  fusion.

### Fusion

```
window_anomaly = max(s_log, s_trace, s_metric)   # over AVAILABLE modalities only
```

`max` encodes "strong evidence in *any* modality is enough to attempt RCA" — which
matches the spike (a fault that surfaces only in metrics, RE2-style, still trips the
gate). A missing modality is dropped from the `max`, **not** fed in as `0`: absence
of traces must not read as "traces say all-clear". Availability is tracked with the
existing `has_logs` / `has_traces` / `has_metrics` flags. **With no available
modalities → abstain** (there is no evidence to diagnose from).

**Caveat `max` hides — the null distribution grows with modality count.** Each `[0,1]`
arm still has some healthy-window spread, and `max` over more arms gives that noise
more chances to trip the gate: a tri-modal (`L+T+M`) deployment and a logs-only
(`L`) deployment do **not** share the same null distribution for
`max(s_log, s_trace, s_metric)`, even though both scores live in `[0, 1]`. So `[0,1]`
membership alone does **not** guarantee one universal threshold works everywhere. We
still start with `max` (it matches the fault model), but the calibration plan below
makes the per-availability check a hard requirement rather than an afterthought.

### Two hard rules (portability)

1. **All parameters are selected on development data, then frozen — the transform
   scales `τ_*` *and* the abstention threshold alike (the `τ_*` are hyperparameters
   too). At deployment time, none of them are adapted from local traffic.** That is
   the scientific contract: the entire mapping from raw signals to abstain/proceed
   is fixed before a deployment ever runs.
2. **No inference-time corpus normalization.** Never normalize by corpus min/max or
   percentiles at inference — that would make `0.25` mean different things in
   different installations, re-introducing the very problem we're removing. The
   `[0, 1]` mapping comes only from the frozen transforms.

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
- **Choose the threshold by a constrained objective, not balanced accuracy** — the
  cost is asymmetric (suppressing a real incident is the expensive mistake):

  ```
  maximize  healthy-window abstention
  subject to  incident recall ≥ 99%
  ```

  The exact floor (99% here) can be measured/debated, but the asymmetry must be
  explicit in the operating point.
- **Report per modality-availability pattern.** Because `max`'s null distribution
  grows with modality count (above), report healthy-abstention and incident-recall
  **separately for `L`, `L+T`, `L+M`, `L+T+M`** (plus the threshold-free AUC, already
  RE3 0.927 / RE2 0.917). If those distributions differ materially, **reject a single
  universal threshold** and calibrate per availability pattern — do not let `[0, 1]`
  membership give a false sense of universality.
- Validate the *frozen* gate on a fresh external corpus (a genuinely healthy
  production-style window, not a pre-injection proxy) before considering default-on.

## Open questions for review

1. **Fusion:** `max` is the natural "any modality" rule; is a softer combiner
   (e.g. noisy-OR) ever wanted, or does `max` + a good threshold suffice? (Start
   with `max`.)
2. **Log-arm input `x_log`:** incident-vs-baseline error-rate elevation is the
   spike's signal; should novelty (a cluster absent from baseline) feed it too, or
   does that re-enter the `change_ratio` entanglement we're avoiding?
3. **Asymmetric cost — resolved:** pick the operating point from the recall side
   (maximize healthy abstention subject to incident recall ≥ ~99%), not balanced
   accuracy. Open only on the exact recall floor.

## Where this sits

The gate completes a clean separation of three distinct questions, each its own
stable signal with its own calibration:

```
1. Is there actually an incident?        ← abstention gate    (this doc)
2. Which service is the root cause?       ← RCA ranker         (#118)
3. How likely is that prediction right?   ← confidence calibrator (#83)
```

Keeping them separate is the point: the gate must not reuse the ranker's cluster
`change_ratio` machinery, and the calibrator's `P(top-1 correct)` is not a
fault-vs-no-fault signal.

_Part of #79 / #118 / #74. No `src/core` change in this doc — design only, pending
review of posture and the score contract above._
