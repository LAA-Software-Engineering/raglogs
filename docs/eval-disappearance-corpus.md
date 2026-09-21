# Disappearance benchmark + frozen pre-F baseline (#184, Phase F prerequisite)

Phase F (#184) attacks the **coverage** blocker: a service that goes silent (stops emitting telemetry)
is never generated as a candidate, so it cannot be localized by any amount of ranking. That phenomenon
**did not exist in any available eval corpus** — the OTel `paymentUnreachable` case has the root with a
*zero* trace footprint (no baseline to establish it was expected), and the synthetic `trace-loc`
families all *increase* activity. So Phase F could not be measured. This benchmark creates the
phenomenon, and this doc **freezes it and the pre-F baseline before Phase F is built** — so Phase F is
measured against a corpus it was not tuned on.

## The disappearance family (`scripts/gen_trace_localization_corpus.py`)

A new fault family **`callee_vanish`** extends the deterministic (seeded) trace-loc generator, written
to its **own** corpus `data/eval-cases/trace-loc-disappearance/` with an independent seed (the existing
24-case `trace-loc` corpus regenerates byte-identically — the freeze is intact). Per topology (shop /
orders / media), 2 variants = **6 cases**. In each:

- the **cause** (a deep callee: `payment` / `ledger` / `storage`) is reachable and emits spans normally
  in the **baseline**, then **goes silent in the incident** — its spans stop entirely (span-rate
  collapse), and its metrics stay flat (no error signal of its own);
- its **caller** (the symptom) errors in the incident because the callee is unreachable.

The only thing that betrays the cause is the **absence** of an expected signal — exactly what
absence-derived candidate generation (Phase F) must recover. `data/` is gitignored, so the corpus is
defined by the committed generator; `tests/unit/test_disappearance_corpus.py` freezes the contract
(baseline spans present, incident spans zero, caller errors, labels), and guards that existing families
are unaffected.

## Frozen pre-F baseline — existing pipeline

`raglogs eval --cases data/eval-cases/trace-loc-disappearance` over the current pipeline (clean DB):

| bucket | of scored | of failures | n |
|---|---|---|---|
| correct | 0% | — | 0 |
| detection | 0% | 0% | 0 |
| **coverage** | **100%** | **100%** | **6** |
| inference | 0% | 0% | 0 |

**Failure rate 100%; every case is a coverage miss** — the vanished cause is never generated as a
candidate. This is the "before" Phase F must move: recovering the cause turns these from `coverage`
into `correct`/`inference`, a directly measurable coverage delta on this exact corpus.

Recorded before Phase F implementation (this PR). Phase F states the after against this table.
