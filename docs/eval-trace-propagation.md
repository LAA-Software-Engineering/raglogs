# Trace-graph propagation reranker — design & evaluation (#118 / #79)

Post-abstention-shelve RCA cycle (2026-09-12). The traces-for-propagation
carve-out (AGENTS.md): close the one gap logs-only provably can't — **propagation
faults**, where the exception surfaces *downstream* of the injected service and only
call-direction can walk back to it.

## The narrow question

The frozen external run (`docs/eval-otel-demo.md`) showed candidate *generation*
usually reaches the truth (top-3 55.6%) but the *ordering* is wrong (top-1 33.3%):
the loud **caller** that only carries the downstream symptom outranks the true
upstream **culprit** it depends on. So the question is strictly about ranking:

> When the symptom appears downstream, can trace topology move the true upstream
> culprit above the caller/symptom service?

## Why this is not `tr_calldir` again

A scalar call-direction feature (#155) — "+0.5 if failing neighbours depend on me"
— was a **wash on RE3 LOSO** and is held as a negative result. It used *static edge
direction alone*. This cycle uses the **actual trace graph** and, crucially,
**temporal precedence**: a root cause degrades *before* the symptoms it propagates
to. Direction alone cannot tell cause from symptom when a caller and its dependency
both error; onset ordering can.

## Mechanism (`src/core/rca/propagation.py`)

The reranker runs *after* the learned ranker, reordering its candidates (it never
rewrites their calibrated scores — a downstream calibrator still reads real
probabilities). Per candidate service `s` that is itself anomalous, for each other
anomalous service `n` connected within `max_hops` in the (undirected) dependency
graph:

- **graph distance** — contribution decays by `proximity_decay` per hop;
- **symptom magnitude** — a louder `n` weighs more (normalised error volume);
- **temporal precedence** — `onset[s] < onset[n]` (by > `min_onset_gap`) ⇒ `n` is a
  downstream symptom of `s` ⇒ boost `s`; `n` earlier ⇒ `s` is itself downstream ⇒
  penalise `s`;
- **propagation direction** (fallback, weight `0` until earned) — when timing is
  tied/missing, treat `s`'s callee (dependency) as the likelier cause.

The summed adjustment is squashed (`tanh`) and applied multiplicatively:
`new = base · (1 + blend · tanh(adj))`, so it reorders **near-ties** rather than
overriding a confident ranker. Empty graph, no onset, or a single anomalous service
⇒ input order unchanged. It is **opt-in** (`RCA_PROPAGATION_RERANK`, default off) and
a no-op without traces.

## Evaluation

`scripts/eval_rca_reranker.py` — leave-one-{system,fault,service}-out on RCAEval
RE2+RE3, every prediction through the committed `RcaRanker` + `propagation_scores`.
Reports top-1 **ranker only** vs **+ rerank**, with a per-fault-class breakdown and a
regression flag on any class where the rerank lowers top-1.

**Gate (brutal, per the directive):** must improve top-1 — especially on
propagation / dependency faults — and must **not materially regress** self-contained
/ code faults. Freeze the hyperparameters from RCAEval, then one-shot on a fresh
external OTel corpus. If it washes like `tr_calldir`, kill it and record the negative
result, exactly as the abstention gate was shelved (`docs/eval-abstention.md`).

## Results

RCAEval RE2 (269 cases) + RE3 (90 cases), 2026-09-12, every prediction through the
committed `RcaRanker` + `propagation_scores`, **default a-priori hyperparameters**
(`blend 0.25 / proximity_decay 0.5 / min_onset_gap 1.0s / direction_weight 0 /
max_hops 2`) — *not* fit to RCAEval, so the lift is not a tuning artifact. Because
`direction_weight=0`, the entire gain comes from **temporal precedence + graph
proximity**, the signal the scalar `tr_calldir` lacked.

**RE3 (code / propagation faults, train-ticket etc.):**

| held-out axis | ranker only | + rerank | Δ |
|---|---|---|---|
| leave-one-system-out | 56/90 = 62.2% | **64/90 = 71.1%** | **+8.9pp** |
| leave-one-fault-out | 73/90 = 81.1% | 73/90 = 81.1% | +0.0 |
| leave-one-service-out | 54/90 = 60.0% | 57/90 = 63.3% | +3.3pp |

The system-out lift (the hardest OOD axis) concentrates in the propagation-heavy
families: f2 53.8%→69.2%, f3 46.2%→65.4%, f4 73.7%→78.9%. **No fault class regresses.**

**RE2 (resource / network faults):**

| held-out axis | ranker only | + rerank | Δ |
|---|---|---|---|
| leave-one-system-out | 206/269 = 76.6% | 208/269 = 77.3% | +0.7pp |
| leave-one-fault-out | 231/269 = 85.9% | 232/269 = 86.2% | +0.4pp |
| leave-one-service-out | 220/269 = 81.8% | 221/269 = 82.2% | +0.4pp |

Smaller, as expected (resource faults have weaker call-graph propagation than code
faults), but the network-propagation classes move most — `loss` (packet loss)
+4.5pp system-out / +4.4pp service-out, `delay` +2.2pp fault-out. The only regression
is a **single case**: `mem` on the service-out axis (40/45→39/45); within noise.

### Verdict — PASS the gate

The rerank **improves top-1, most on propagation/dependency faults** (RE3 system-out
+8.9pp; network faults on RE2), and **does not materially regress code faults** (one
single-case memory-fault flip). It clears the brutal gate with un-tuned defaults, so
the hyperparameters are frozen as the module defaults. This is the first change in the
post-frozen arc — after the `tr_calldir` wash (#155) and the shelved abstention gate
(`docs/eval-abstention.md`) — to move top-1 with no material regression.

**Next (one-shot, per the discipline):** validate frozen on a fresh external OTel
corpus. Ships **opt-in** (`RCA_PROPAGATION_RERANK`, default off) until that external
number is in. If it fails to transfer, it is killed and recorded as a negative result
like the others — but on RCAEval it earns its place.

## Evidence source: logs → traces (error-status), and what OTel revealed

The v1 above derived onset + symptom magnitude from **error logs**. The spent OTel
corpus (`data/eval-cases/otel`, now dev/diagnostic evidence, no longer external
validation) showed that made the reranker a **complete no-op on OTel**: the OTel Demo
emits ~12 error-level log lines in the whole corpus, so with no log onset there was
nothing to fire on — the same "logs are silent on OTel" wall the abstention work hit.

The fix extends the *evidence source*, not the reranker logic
(:func:`trace_symptoms` + :func:`combine_log_trace_evidence`): per service, use log
onset + log-error magnitude where logs fire, else fall back to **trace ERROR-status**
spans (the trace analog of an error log — first error span's time + error-span count).

Latency-excursion was evaluated as an additional trace trigger and **dropped**: on
RCAEval LOSO it materially regressed resource faults (RE2 system-out 76.6%→75.5%,
`mem` −8.9pp) because a resource fault balloons latency across every downstream
service, so "earliest excursion" is noise. Error-status is clean, and RCAEval traces
carry essentially none (all `statusCode` 0/UNSET), so:

- **RCAEval LOSO is preserved *by construction*** — trace evidence is empty there (no
  error-status spans), so `combine_log_trace_evidence` yields onset + anomaly identical
  to v1's log-only path and the reranker's inputs are unchanged. The **+8.9pp RE3
  system-out** lift is reproduced. Measured top-1 matches v1 within run-to-run GBM
  row-ordering variance (±1 case in the *base* ranker, which is retrained per fold): this
  run's RE2 was **+0.4pp on all three axes** (system-out 207/269) vs v1's +0.7/+0.4/+0.4
  (208/269 system-out) — a one-case wobble in the base ranker, not a reranker effect. No
  new regression. Gate: **passed.**
- **Spent OTel: the reranker now activates** — error-status spans exist for the payment
  cases, and `otel_paymentUnreachable` reorders, pulling `checkout` (payment's caller)
  into the top-3 over the unrelated `shipping`: directionally sane.

But **top-1 did not move on OTel**, for a reason *outside* the reranker: in the
propagation cases the true culprit is **not in the candidate set at all** —
`payment` errors only in trace status, and `build_candidates` sources candidates from
log-error / trace-rate / metric features, not span status, so `payment` is never
generated. The reranker can only reorder candidates that exist. **This is a
candidate-generation gap, not a ranking one, and it is the next lever** (add
trace-error-status as a candidate source + ranker feature). The reranker extension
is safe and correct as far as it goes — RCAEval untouched, OTel now activates instead
of no-op — and it makes the next bottleneck legible.
