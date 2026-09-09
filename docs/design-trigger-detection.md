# Trigger detection redesign (#82) — scoped against the multi-modal architecture

## Problem (today)

Trigger detection is **12 regexes + a 30-minute proximity check**, and the causal
claim is a single line in `evidence.py`:

```python
if 0 <= (primary.first_seen - earliest_trigger.timestamp)/60 <= 30:
    items.append("First error spike occurred Nm after <trigger>")
```

Three failures, from the issue:

1. **Detection can't see unannounced or unanticipated triggers.** `TRIGGER_PATTERNS`
   misses `Helm upgrade succeeded`, `scaled replicaset`, `certificate renewed`,
   `traffic shifted to canary`, anything in another language/house style — and
   **most real triggers write no log line at all** (all 270 RE2 resource/network
   faults announce nothing).
2. **No linkage.** A deploy of service X is offered as the cause of errors confined
   to unrelated service Y. `trace_id` is parsed, indexed, and unused in explain.
3. **Asymmetric, dangerous confidence.** A regex match is worth 2/8 points and is
   the *sole* gate to `"high"` — a stray INFO `token expired` manufactures high
   confidence for a wrong story. Confidently wrong is worse than "I don't know".

## Direction (issue) → what we now have

The multi-modal work (#118) added exactly the ingredients #82 asked for:

| #82 direction | mechanism now available |
|---|---|
| Rare-event correlation | `ClusterData.baseline_count` / `change_ratio` / `first_seen` (in-job baseline, #115) — a fingerprint with `baseline_count == 0` that first appears at error onset is a candidate *by definition* |
| Service/dependency linkage | `trace_spans` (ingested #126) — build the caller→callee graph from `parent_span_id` and check the trigger service reaches the erroring service |
| Control comparison | `src/core/compare/differ.py` window diffing — a candidate also present in a comparable healthy window is background noise |
| Demote the regex list | `infer_trigger_type` stays as a **type classifier + confidence tiebreaker**, not the detector |
| Separate found-vs-explains | report `trigger_found` and `trigger_explains` independently |

## Design

### 1. Candidates by rarity, not regex

A trigger candidate is any **rare** fingerprint whose onset precedes the error
onset in the window: `baseline_count == 0` (or `change_ratio ≥ rare_threshold`),
ranked by rarity × onset-earliness relative to `primary.first_seen`. The regex
list is no longer the detector — it only *labels* a candidate's type
(`infer_trigger_type`) and breaks ties. This generalises to any phrasing/language,
and to unannounced faults it surfaces the earliest rare *symptom* onset as the
correlated-change marker (honestly a symptom, not a root-cause line — see §4).

### 2. Linkage gate (traces)

`src/core/rca/linkage.py` builds a service dependency graph from `trace_spans` in
the window (`parent_span_id → span_id`, mapped to `service`), and exposes
`services_linked(a, b)` = a and b are the same service, or connected (either
direction) in the caller→callee graph. A candidate whose service is **not linked**
to the erroring/root-cause service is demoted to `trigger_found` only — never
offered as the cause. With no traces (logs-only scope) linkage is unavailable and
the gate is **skipped, not failed** (graceful fallback — same discipline as the
ranker), falling back to same-service overlap.

### 3. Control comparison

Reuse `differ.diff_windows` (or `baseline_count`) so a candidate that also fires in
a comparable healthy/baseline window is dropped as noise. `baseline_count == 0`
already encodes "absent from the in-job baseline"; the differ adds an explicit
healthy-window control when one is available.

### 4. Separate "found" from "explains"

`EvidencePacket` grows two orthogonal signals:

- **`trigger_found`** — a rare change occurred near onset (may be unlinked).
- **`trigger_explains`** — that change is *rare*, *linked* to the errors, and
  *control-clean*. Only this promotes confidence.

"Errors began at 14:09; no correlated change found" becomes a first-class, honest
output — a feature, not a gap.

### 5. Confidence fix (the dangerous gate)

`"high"` requires `trigger_explains`, not merely a regex match. A single
unvalidated regex hit no longer reaches `"high"` — it may still contribute a small
tiebreak point, but the gate is the validated (rare + linked + control-clean)
trigger. All thresholds stay configurable (#116), defaults documented.

## Phasing

- **T1 (build first, per the plan):** rare-event candidate extraction + the
  trace/service linkage module (`rca/linkage.py`) — no confidence change yet.
- **T2:** control comparison + the confidence-gate fix (`trigger_explains`).
- **T3:** measure on RCAEval — `trigger_hit` (injection-time tolerance) and the
  no-false-trigger property, **before/after, RE2 + RE3, vs the trivial baseline**.
- **T4 (deferred — issue AC 5):** the confounded OTel acceptance test (real deploy
  + injected fault in one window selects the correct trigger) stays **pending #79's
  live run**, since it needs the generated OTel corpus.

## Acceptance criteria mapping

- *Finds candidates on RE2 where no trigger line exists* → §1 rare-onset candidates.
- *No trigger in an unrelated service* → §2 linkage gate.
- *Confidence no longer reaches "high" on one regex* → §5.
- *Eval lift before/after on RE2 + RE3* → T3.
- *Confounded OTel case selects the right trigger* → T4 (pending #79).
