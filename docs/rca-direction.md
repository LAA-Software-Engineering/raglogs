# RCA direction: logs for manifestation, traces/metrics for causal localization

_Pivot decision for #118 / epic #74, 2026-09-08. Grounded in measured results,
not architecture preference._

## What we measured

raglogs' root-cause selection reached **parity with the trivial
most-frequent-error baseline** on two independent corpora (RE3 28.9%, RE2 8.0%)
and **no logs-only lever beat it robustly**:

| logs-only approach | RE3 |
|---|---|
| importance-score ranking | ✗ (below baseline) |
| volume (most-frequent error) | baseline — 28.9% |
| novelty selection | ✗ |
| anomaly (change-ratio) | ✗ |
| onset (first-seen) | ✗ |
| anomaly + onset | ✗ (wash; RE3 +1 case / RE2 −1 case) |
| stack-trace content | partial — 34.4% corpus, but **fails leave-one-system-out** |

The stack-trace spike (#118, `docs/spike-stacktrace-rca.md`) is the clincher: it
works on **self-contained** faults (online-boutique, a service throwing its own
exception: 60%) but collapses on **propagation** faults (train-ticket: 3.3%),
where the exception surfaces *downstream* of the injected service.

## The conclusion

**Logs tell you where a failure *manifested*; only call-direction (traces) tells
you where it *originated*.** Volume finds the loudest symptom; stack content
finds where the exception was thrown — both are manifestation signals. On
propagation faults the injected service often throws *nothing itself*; it
corrupts a request and a downstream service throws. No logs-only signal can walk
that edge backward. This is not a tuning problem; it is an information problem.

## Direction (the pivot)

Keep logs as the manifestation/evidence layer; add **traces (and metrics)** as
the causal-localization layer. Sequenced, each phase gated on a measured lift
(leave-one-service / -fault / -system-out — random splits will lie):

- **C1 — ingest RCAEval traces/metrics** (RE2/RE3 ship them; the freeze carve-out
  in `AGENTS.md` covers this). Logs-only stays the default; traces are additive.
- **C2 — dependency graph + propagation scoring.** Build the service call graph
  from traces; score candidates by failure propagation (temporal precedence +
  downstream-failure count + trace error/latency deltas); reverse-propagate.
  Target: beat 28.9%/8.0% *and* hold under leave-one-system-out (train-ticket is
  the brutal test).
- **D — feature dataset.** One row per candidate service per incident: the
  logs-only features (volume, anomaly, onset, stack-origin) **plus** the trace
  features. Label = is-root-cause.
- **E — tiny learned ranker** (logistic regression / gradient-boosted trees,
  learning-to-rank) → `P(service = root cause | evidence)`, feeding calibrated
  confidence (#83). LLM, if any, only *re-ranks* pre-computed evidence.

## Guardrails (unchanged)

- Every phase lands behind the eval harness with the lift delta stated.
- Leave-one-*-out evaluation only; no random splits.
- raglogs stays evidence-based: candidate generation → features → ranking →
  calibrated confidence. No raw-logs-into-an-LLM.
- The rest of the #88 freeze (auth modes, client languages, unrelated API
  surface) still stands; only metrics/traces-for-RCA is unfrozen.

_Supersedes the "is 28.9% a ceiling or a gap?" question in #118 with the
answer: a logs-only ceiling for propagation faults; the gap is in traces._
