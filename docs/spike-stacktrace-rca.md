# SPIKE result: stack-trace content for RE3 root cause (#118)

**Question:** does parsing stack-trace *content* localize the RE3 root cause
better than error *volume* (the ~28.9% trivial baseline)?

**Method:** `scripts/spike_stacktrace_rca.py` — cheap, standalone, no `src/core`
changes. For each of the 90 RE3 cases, over the incident window `[inject,
inject+600s]`:

- **volume** — service owning the largest single `(service, fingerprint)`
  error group (reproduces the trivial baseline; measured 25.6%, ≈ eval's 28.9%);
- **stack** — service owning the most *originating* stack-trace lines (a thrown
  exception **with real frames** — `\n\tat …`, `Traceback`, `panic:`,
  `Exception in thread`, `nested exception is` — **not** a relayed `500` / JSON
  error body, which have no frame);
- **stack+volume** — prefer a service with originating stack traces, else volume.

## Result (RE3, 90 cases)

| selector | accuracy |
|---|---|
| volume (≈ trivial baseline) | 25.6% (23/90) |
| stack alone | 26.7% (24/90) |
| **stack + volume** | **34.4% (31/90)** |

Confusion (volume vs stack): both-correct 9, volume-only 14, stack-only 15,
both-wrong 52 → the two signals are **complementary** (stack fixes 15 cases
volume misses).

### Per system — the decisive cut (leave-one-system-out)

| system | n | volume | stack | stack+volume |
|---|---|---|---|---|
| online-boutique | 30 | 20.0% | 60.0% | **60.0%** |
| sock-shop | 30 | 30.0% | 20.0% | **40.0%** |
| train-ticket | 30 | 26.7% | 0.0% | **3.3%** |

## Conclusion — real signal, but not robust

- **Logs-only stack content genuinely localizes *self-contained* faults** — a
  service that throws its own exception (online-boutique's gRPC/startup crashes:
  +40pp). The "logs are a hard ceiling" framing was too pessimistic.
- **It fails on *propagation* faults** — train-ticket's layered services surface
  the exception *downstream* of the injected service, so stack-first prediction
  is reliably wrong and the ensemble collapses to **3.3%** (below the 26.7%
  volume baseline). This **fails leave-one-system-out**: deployed globally it
  would wreck train-ticket-like systems.
- The unsolved part is distinguishing self-contained from propagation faults at
  inference — which needs **call-direction / dependency topology**, i.e. traces,
  not logs.

**Sharpest form of the epic thesis (#118/#74):** logs suffice for self-contained
faults; propagation faults require trace/dependency direction for causal
localization. The 34.4% corpus average is an online-boutique artifact, not a
deployable logs-only win.

## Body of negative/partial evidence on logs-only RE3 root cause

| approach | result |
|---|---|
| importance-score ranking | ✗ (below baseline) |
| volume (most-frequent error) | baseline (28.9%) |
| novelty selection | ✗ |
| anomaly (change-ratio) | ✗ |
| onset (first-seen) | ✗ |
| anomaly + onset | ✗ (wash) |
| stack-trace content | partial — self-contained faults only, fails LOSO |

_Part of #118 / #74._
