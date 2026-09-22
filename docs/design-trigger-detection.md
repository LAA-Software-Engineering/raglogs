# Trigger detection redesign (#82) — scoped against the multi-modal architecture

## Status (2026-09-22): mechanism implemented and measured; default promotion DEFERRED

T1 (rare-event candidates + trace/service linkage) and T2 (`trigger_found` /
`trigger_explains` split + confidence fix) are implemented and `rare_event` is
available via `settings.trigger_mode`. **The default stays `regex`.** The T3
measurement below settles why: rare-event detection has excellent *recall*, but no
current variant has acceptable *specificity* to be the default on real telemetry.

**Frozen measurement** (`raglogs eval` + a real-OTel default-safety check;
root-cause accuracy unchanged and `rare_event` never returns `"high"` throughout):

| corpus | signal | regex | rare_event |
|---|---|---|---|
| trace-loc (24) | trigger-accuracy | 0% | **100%** |
| trace-loc-disappearance (6) | trigger-accuracy | 0% | **100%** |
| otel-fresh (9 pos / 12 neg) | positive `trigger_hit` | 11% | **100%** |
| otel-fresh negatives | surfaced-trigger rate (want low) | 0% | **100%** (unfiltered) |
| otel-fresh negatives | surfaced-trigger, linked-only gate | — | 42% |
| otel-fresh positives | `trigger_hit`, linked-only gate | — | **56%** |

The read:

- **Recall is strong and real.** The 12 regexes detect *nothing* on injected /
  unannounced faults (they write no deploy line — failure #1); rare-event
  correlation recovers the trigger on 100% of trace-loc and real-OTel positives.
- **Specificity is the blocker.** Unfiltered, rare-event surfaces a trigger on
  ~100% of healthy windows (real telemetry always has *some* rare fingerprint).
- **Linkage is not a usable causal gate — yet.** Requiring a trace-graph link to the
  erroring service before surfacing a candidate cut the healthy-window rate to 42%,
  but also **destroyed recall (100% → 56%)**: on ~44% of real-OTel positives the true
  trigger is on a service the (incomplete) trace graph doesn't connect. So linkage is
  kept as *evidence attached to a candidate* (`trigger_explains`), never a
  prerequisite for the candidate to exist.

**Conclusion (a useful negative result):** *rare-event detection has good recall;
trace linkage currently has inadequate recall to serve as a causal gate.* Default
promotion is deferred pending a specificity signal that does not sacrifice recall —
which points back to improving the real-telemetry call-edge model (then rerun this
exact frozen gate). RE2/RE3 external measurement remains available via
`make eval-corpus`; T4 (the confounded OTel acceptance case) stays pending #79.

### Landed regardless of the default decision

- The **onset gate** (`primary is None or primary.first_seen is None`): closes only
  the **trivial empty-window case** — a window with *zero* clusters surfaces no
  trigger. It does **not** cover the realistic healthy case: `select_primary_cluster`
  falls back to the highest-*volume* cluster even when nothing is error-level, so a
  window with ordinary non-error traffic still gets a fallback "primary" with a real
  `first_seen` and still surfaces a trigger. That realistic healthy-noise case — the
  `100%` unfiltered surfaced-trigger rate in the table above — is exactly why the
  default was **not** promoted; the onset gate does not solve it.
- The **combined-set ordering contract**: log + metric candidates are surfaced
  together with linked candidates first, so `candidates[0]` is the most defensible.
- The **`trigger_found` vs `trigger_explains`** split as separate metadata.
- The corrected confidence semantics (see §5).

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

**As shipped, the fix is stricter than "gate high on `trigger_explains`".** T3
showed a rare (+ often linked) trigger fires on nearly every log-announced incident,
so it does *not* validate the explanation — gating `"high"` on it over-promoted
(RE3: 56/90 "high" at 21% accuracy, below base rate). So in `rare_event` mode the
rare-event trigger contributes **no** confidence points at all, and the label is
**capped at `"medium-high"` — `rare_event` mode never returns `"high"`** until
confidence is calibrated against measured accuracy (#83 / Phase D). `trigger_found`
/ `trigger_explains` are reported as honest evidence signals, not confidence
inflators. (Legacy `regex` mode keeps the old "high requires a trigger" gate,
byte-identical.) All thresholds stay configurable (#116).

### 6. Candidate ordering (combined log + metric set)

`_rare_event_triggers` produces one candidate list from two sources — rare log
fingerprints (ranked by rarity x onset-earliness) and metric anomaly onsets. The
combined set is ordered by a single documented contract: **a candidate whose
service is linked to the erroring service sorts first** (stable, so within-group
order is preserved), so the displayed/evaluated top candidate (`candidates[0]`) is
a linked one whenever any exists — never an unrelated rare change that merely sorted
earlier. `trigger_explains` remains "any candidate links to the errors".

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
