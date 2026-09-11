# SPIKE result: abstention on healthy windows (#118 gen-2 / #79 follow-up)

**Question (the one that gates the build):** the frozen OTel-Demo run (#79) abstained
on **0/2** healthy windows — raglogs invents an incident from healthy background
traffic. Today it abstains only when a window has *no clusters at all*
(`src/core/explain/summarizer.py`), so any window with baseline-level errors gets a
confident explanation. Can a **window-level anomaly score** — "is anything actually
elevated vs the baseline, in any modality?" — separate real incidents from healthy
windows, and does it hold across systems?

**Method (RCAEval-only):** `scripts/spike_abstention_rca.py` — standalone, no
`src/core`. RCAEval has 720–900 s of pre-injection data per case, so a genuinely
healthy window with its own healthy baseline fits entirely before the fault:

| case | window | baseline |
|---|---|---|
| **incident** (positive) | `[inject, inject+W]` | `[inject−B, inject]` |
| **healthy** (negative) | `[inject−B, inject]` | `[inject−2B, inject−B]` |

`W = B = 300 s`. Window-anomaly score = **max over services** of the strongest
single-modality deviation in the window: `max(log-error-rate elevation, trace-rate
deviation, metric mean-change)`. A real incident has at least one elevated
(service, modality); a healthy window should have none. Scored with ROC-AUC
(`P(incident score > healthy score)`) and the balanced-accuracy break-even
threshold.

## Result — RE3, 90 incident / 90 healthy, per system

| | overall | online-boutique | sock-shop | train-ticket |
|---|---|---|---|---|
| **ROC-AUC** (incident > healthy) | **0.927** | 0.960 | 0.907 | 0.957 |

Break-even threshold → **89.4% balanced accuracy**: explains **79/90** incidents,
correctly abstains on **82/90** healthy windows.

- incident score: median `1.7e8`, healthy score: median `1.36`.
- The separation holds on **every system** (AUC 0.91–0.96), so it is not a
  per-system quirk — the same signal that localises faults also tells "fault vs no
  fault".

## What this establishes

- **Abstention is viable and generalizes.** A single window-anomaly threshold
  separates incidents from healthy windows at AUC 0.93 across three systems — the
  direct fix for the frozen-eval false alarms (0/2 abstention).
- It is **multi-modal by construction**: the score maxes over log/trace/metric
  deviations, so a fault that only shows in metrics (RE2-style) can still trip it —
  though that arm is not yet measured (see caveats).

## Honest caveats (what a productized version must fix)

- **The raw score is not portable.** Its scale is dominated by novel-error-rate
  blow-up: a service with errors in the window and ~none in the baseline gives a
  ratio in the 1e8 range (baseline ≈ 0). That is a strong "new errors appeared"
  signal but the *threshold* (`~4.7e7` here) is a raw ratio, not deployment-neutral.
  A real abstention gate needs a **bounded/robust score** (log-transform or a
  per-modality robust z-score) and a threshold **learned from held-out reliability**
  (not fit on the same cases) — same discipline as [[project-rcaeval-re3-baseline]]
  confidence work (#83).
- **RE3 only** (log-visible code faults). RE2 (resource/network) leans on the metric
  arm; healthy-vs-incident separability there is **not yet measured** and is the
  required next check before wiring abstention into `src/core`.
- 90/90 single run; `W = 300 s` fixed. The pre-injection window is a *proxy* for a
  healthy production window — real healthy traffic may be noisier.
- Abstention is a **window-anomaly** gate, orthogonal to the root-cause ranker: it
  answers "is there an incident at all", the ranker answers "which service". They
  compose (abstain → skip ranking / return insufficient evidence).

_Part of #118 / #74. Numbers 2026-09-11. Headline: **AUC 0.927 / 89% balanced
accuracy** separating incidents from healthy pre-injection windows on RE3, all
three systems 0.91–0.96._
