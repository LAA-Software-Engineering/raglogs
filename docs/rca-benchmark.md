# RCA benchmark — where raglogs sits (SPIKE #118, Phase A, first cut)

**Question this answers:** is raglogs' ~28.9% (RE3) / 8.0% (RE2) top-1 root-cause
accuracy near the *ceiling* for these corpora, or a *gap* we're leaving on the
table?

**Method (this cut):** cite the published numbers for RCAEval's own reproducible
baselines rather than re-running them (a full local re-run is the follow-up —
Phase A option B). **Numbers below are directional, not apples-to-apples** — see
the caveats. Sources are linked per row.

## The table

| Method | Modality | RE2 (resource/network) | RE3 (code faults) | Metric | Source |
|---|---|---|---|---|---|
| Trivial (most-frequent error cluster) | logs | 8.0% | 28.9% | top-1 | this repo (`raglogs eval`) |
| **raglogs** (current) | logs | **8.0%** | **28.9%** | top-1 | this repo |
| BARO | metrics | Avg@5 **0.63–1.0** on Train Ticket (per fault: CPU .72 / MEM .99 / DISK 1.0 / SOCKET .83 / DELAY .63 / LOSS .64) | weak on code faults (metric-based) | Avg@5 | [RCAEval README](https://github.com/phamquiluan/RCAEval) |
| BARO (avg over RCAEval) | metrics | — | — | Top@1 **0.24** (avg across corpus) | [arXiv 2510.04711](https://arxiv.org/html/2510.04711v2) |
| "Simple baseline" (propagation critique) | multi | — | **0.83** on RE3-TT / RE3-SS subsets (vs BARO 0.50 / 0.00) | (subset) | [arXiv 2510.04711](https://arxiv.org/html/2510.04711v2) |
| 15+ others (CIRCA, RCD, MicroRank, TraceRCA, CausalRCA, Multi-source *, EventADL, …) | metrics / traces / multi | reported in paper Table 6 — **not yet transcribed** | reported — **not yet transcribed** | AC@1/AC@3/Avg@5 | [RCAEval paper](https://arxiv.org/abs/2412.17015) |

## Caveats (important)

1. **Metric mismatch.** RCAEval reports **AC@k / Avg@5** (credit if the true
   cause is in the top-k, usually k=5). raglogs' numbers are **top-1**. Avg@5 is
   strictly more lenient, so BARO's 0.63–1.0 is *not* directly comparable to our
   8% — but the gap is far larger than the metric difference can explain.
2. **Modality.** Every strong RCAEval method consumes **metrics and/or traces**.
   RCAEval RE2/RE3 ship those (RE3: ~2M log lines **+ 4.5M traces + 68–322
   metrics** per system). raglogs currently ingests **logs only**, and RE3 is
   explicitly *designed* for multi-source RCA (stack traces in logs, response
   codes in traces).
3. **Secondary source.** The Top@1 0.24 and RE3-subset 0.00–0.83 figures come
   from a *critique* paper (2510.04711), not RCAEval's own Table 6; treat as
   indicative until confirmed by a local run.
4. Per-system vs per-corpus: some figures are per-system (Train Ticket) or
   per-subset, not the 90-case RE3 / 270-case RE2 aggregate raglogs reports.

## What this says about ceiling vs gap

- **RE2 (resource/network faults): mostly a gap, and the lever is metrics.** BARO
  (metric-based) reports Avg@5 0.63–1.0 on RE2 Train Ticket; raglogs gets 8%
  top-1. Even discounting the metric difference, resource faults (CPU/mem/disk)
  announce themselves in **metrics**, not error logs — which is exactly why
  every log-only lever we tried was a wash on RE2. Logs-only is near its ceiling
  here; the headroom is in metrics (frozen; needs a product-scope decision).
- **RE3 (code faults): genuinely hard even for SOTA, and the signal is partly in
  logs we under-use.** Methods struggle on RE3 (BARO 0.00–0.50 on subsets), and
  the corpus is built for multi-source diagnosis via **stack-trace content in
  logs** and **response codes in traces**. raglogs ingests the stack-trace logs
  but only *fingerprint-counts* them — it never reads their content. So RE3 has a
  plausible **logs-only** lever we have not tried (stack-trace / exception-type
  analysis to attribute the faulting service), *plus* a trace lever.
- **Overall:** 28.9% / 8.0% is a **logs-only-volume ceiling**, consistent with
  every reweighting/novelty/onset experiment being a wash. The levers are (a)
  metrics + traces (the bulk of the gap, frozen — product decision), and (b)
  stack-trace *content* analysis on RE3 (logs-only, likely freeze-compatible and
  worth a measured attempt).

## Follow-up (Phase A option B, if we want apples-to-apples)

Stand up RCAEval's package, run a subset of its methods (BARO, CIRCA, RCD,
MicroRank) on RE2/RE3 with metrics+traces, and record **top-1** (not Avg@5)
alongside raglogs to remove caveats 1–4. Heavier (new harness, heavy deps, slow
methods); do it only if this first cut leaves the ceiling-vs-gap call unclear —
it currently does not.

_Part of #118 (SPIKE) / #74 (epic). Numbers transcribed 2026-09-08; correct the
"not yet transcribed" rows from RCAEval Table 6 when doing option B._
