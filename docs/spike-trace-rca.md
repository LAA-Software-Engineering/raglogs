# SPIKE result: trace anomaly for RE3 root cause (#118, Phase C)

**Question:** do traces localize the RE3 root cause where logs cannot — the
*propagation* faults (train-ticket) that every logs-only lever missed?

**Method:** `scripts/spike_trace_rca.py` — cheap, standalone, no `src/core`
changes. RE3 `traces.parquet` has **no `statusCode`** (100% null), so failure is
inferred from *change vs a pre-injection baseline* (BARO-style) over
trace-derived per-service signals, noise-filtered by a minimum incident span
count:

- **rate** — spans/sec, incident/baseline (a fault often makes the injected
  service get retried/hammered while downstream traffic collapses)
- **dur** — p95 span duration, incident/baseline (latency anomaly)

## Result (RE3, 60 cases — sock-shop excluded, see below)

| selector | accuracy |
|---|---|
| logs volume (baseline) | 28.9% |
| trace rate | 30.0% (18/60) |
| trace dur | 25.0% (15/60) |

### Per system — the decisive, complementary cut

| system | logs volume | logs stack | trace rate |
|---|---|---|---|
| online-boutique | 20% | **60%** | 10% |
| train-ticket | 27% | 0% | **50%** |

## Findings

- **Traces recover the propagation faults logs cannot.** On train-ticket —
  where volume got ~27% and stack-trace content got **0%** — trace rate-anomaly
  gets **50%**. The injected service is the one whose call-rate *rose* while
  downstream traffic collapsed (the fault blocks the services below it).
- **Traces fail where logs win.** On online-boutique (self-contained gRPC/
  startup crashes) stack content gets 60%, traces 10%. The exception is right
  there in the service's own logs; the trace anomaly is diffuse.
- **They are complementary by fault class.** Neither modality is good
  everywhere; an oracle that used logs for self-contained faults and traces for
  propagation faults would plausibly reach ~55–60% on both systems — roughly 2×
  the 28.9% logs-only baseline. The open problem is *routing* (deciding which
  signal to trust per case) without a label.

## Data limitation

**sock-shop ships no `traces.parquet`** (0/30 cases — only logs + metrics). So
the trace lever covers online-boutique and train-ticket; sock-shop's third
modality is **metrics**. A full multi-modal RCA needs all three.

## So what

This is the empirical justification for the pivot (#118, `docs/rca-direction.md`):
logs localize *manifestation* / self-contained faults; **traces localize
*causal origin* for propagation faults**. Confirms the direction and motivates
building trace ingestion + dependency/propagation scoring for real, with a
logs+traces ensemble routed by fault class, evaluated leave-one-system-out.

_Part of #118 / #74. Numbers 2026-09-08._
