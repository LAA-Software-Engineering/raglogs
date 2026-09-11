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

## Result — RE3 (code faults) and RE2 (resource/network faults)

| suite | cases | ROC-AUC | balanced-acc @ break-even | per-system AUC (ob / ss / tt) |
|---|---|---|---|---|
| **RE3** | 90 / 90 | **0.927** | 89.4% (explain 79/90, abstain 82/90) | 0.960 / 0.907 / 0.957 |
| **RE2** | 270 / 270 | **0.917** | 93.3% (explain 263/270, abstain 241/270) | 0.875 / 0.983 / 0.890 |

The separation holds on **every system in both suites** (AUC 0.88–0.98), so it is not
a per-system or per-fault-class quirk — the same signal that localises faults also
tells "fault vs no fault".

- RE3 incident score median `1.7e8` vs healthy `1.36` — RE3 (code faults) trips the
  **log** arm, where a novel-error ratio against a ~0 baseline explodes.
- RE2 incident score median `101.75` vs healthy `0.66`, break-even threshold `3.65`
  — RE2 (resource/network faults) trips the **metric/trace** arms, whose bounded
  ratios give a *far more reasonable* threshold. This is the important cross-check:
  abstention is not a log-only trick; it catches the metric-driven faults RE2 is
  built from, and those arms look more portable than the log arm.

## What this establishes

- **Abstention is viable and generalizes.** A single window-anomaly threshold
  separates incidents from healthy windows at AUC 0.93 across three systems — the
  direct fix for the frozen-eval false alarms (0/2 abstention).
- It is **multi-modal by construction**: the score maxes over log/trace/metric
  deviations, so a fault that only shows in metrics (RE2-style) can still trip it —
  confirmed by the RE2 run above (AUC 0.917), where the metric/trace arms carry the
  separation.

## Honest caveats (what a productized version must fix)

- **The raw score is not portable.** Its scale is dominated by novel-error-rate
  blow-up: a service with errors in the window and ~none in the baseline gives a
  ratio in the 1e8 range (baseline ≈ 0). That is a strong "new errors appeared"
  signal but the *threshold* (`~4.7e7` here) is a raw ratio, not deployment-neutral.
  A real abstention gate needs a **bounded/robust score** (log-transform or a
  per-modality robust z-score) and a threshold **learned from held-out reliability**
  (not fit on the same cases) — same discipline as [[project-rcaeval-re3-baseline]]
  confidence work (#83).
- **Both suites now measured** (RE3 code faults *and* RE2 resource/network faults —
  the metric arm carries RE2 at AUC 0.917). The remaining external check is a
  genuinely healthy *production* window on a fresh corpus, not a pre-injection proxy.
- Single run; `W = 300 s` fixed. The pre-injection window is a *proxy* for a
  healthy production window — real healthy traffic may be noisier.
- Abstention is a **window-anomaly** gate, orthogonal to the root-cause ranker: it
  answers "is there an incident at all", the ranker answers "which service". They
  compose (abstain → skip ranking / return insufficient evidence).

_Part of #118 / #74. Numbers 2026-09-11. Headline: **RE3 AUC 0.927 / RE2 AUC 0.917**
separating incidents from healthy pre-injection windows, all six system×suite combos
0.88–0.98; break-even balanced accuracy 89–93%._
