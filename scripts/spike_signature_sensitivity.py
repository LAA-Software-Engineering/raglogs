#!/usr/bin/env python3
"""Spike (#181): signature-equality sensitivity before Phase D (#177), v2.

Time-boxed investigation, **not production code**. It de-risks the structural assumption
behind Phase D/E: that IDENTIFIED / NON_IDENTIFIABLE / UNCERTAIN can rest on exact
categorical equality of *causal-hypothesis prediction* signatures over *usable* observables.

Review of v1 (#195) was correct that v1 did the wrong thing: it partitioned **services by
the telemetry they emitted** and consulted ground truth to *build* the label. v2 does the
structural thing the epic requires, with a **prototype** expectation model standing in for the
still-open Phase A/C:

  1. Hypothesis space H = {(service, mode) : mode ∈ {error, latency}} — "service s is the root
     cause, failing in mode m".
  2. Forward prediction S(h): a fault at a callee propagates **up** the caller chain (a caller
     that calls a failing/slow dependency sees errors/latency), cause-first. So the affected set
     is {s} ∪ transitive-callers(s); each affected service is predicted to show the mode signal,
     with a predicted onset rank = hop distance from the cause. This is a deliberately simple,
     ground-truth-free causal model — its fidelity is itself a reported finding.
  3. `F_usable` is an **explicit availability set** (which observable types were measured),
     swept as scenarios — not derived from whether service values happened to vary.
  4. A hypothesis **survives** if its predicted observable state equals the observed state over
     F_usable (onset compared as a bucketed order within a tolerance). Survivors are partitioned
     into ~_O classes by their predicted signature over F_usable.
  5. **Outcome (exactly #177):** IDENTIFIED iff one surviving class that is a singleton;
     NON_IDENTIFIABLE iff one surviving class with ≥2 hypotheses; UNCERTAIN iff ≥2 classes;
     (NONE iff no survivor — a prototype-model-fidelity failure, reported separately).
  6. Ground truth (root_cause + fault-type→mode) is used **only** to score, per case, whether the
     true hypothesis was retained and whether the reported resolution is correct — never to build
     the label.

Run (after `python scripts/gen_trace_localization_corpus.py`):
    python scripts/spike_signature_sensitivity.py
"""
from __future__ import annotations

import glob
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime

import yaml

BASE = "data/eval-cases/trace-loc"
ERROR_STATUS = "2"
DIMS = ("err_log", "err_span", "err_rate", "lat")  # presence observables
MODES = ("error", "latency")

# Availability scenarios (F_usable) — explicit "which observable types were measured".
# onset is a separate ordinal dimension, available only when spans/metrics timing is kept.
AVAIL_SCENARIOS = {
    "all": ("err_log", "err_span", "err_rate", "lat", "onset"),
    "no_onset": ("err_log", "err_span", "err_rate", "lat"),
    "metrics_only": ("err_rate", "lat", "onset"),
    "spans_only": ("err_span", "onset"),
    "logs_only": ("err_log",),
}


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def load_case(d):
    case = yaml.safe_load(open(os.path.join(d, "case.yaml")))
    tl = case["trace_localization"]
    wstart = case["window"]["start"]
    edges = [tuple(e) for e in tl.get("edges", [])]
    services = sorted({s for e in edges for s in e} | {tl["root_cause"]})

    mb, mw, mw_ts = defaultdict(list), defaultdict(list), defaultdict(list)
    for line in open(os.path.join(d, "metrics.jsonl")):
        r = json.loads(line)
        key = (r["service"], r["metric"])
        if r["ts"] < wstart:
            mb[key].append(r["value"])
        else:
            mw[key].append(r["value"])
            mw_ts[key].append((r["ts"], r["value"]))

    span_err = defaultdict(int)
    onset = {}  # service -> earliest in-window error/degradation timestamp (float)
    for line in open(os.path.join(d, "spans.jsonl")):
        r = json.loads(line)
        if r["start_time"] >= wstart and str(r.get("status_code")) == ERROR_STATUS:
            span_err[r["service"]] += 1
            t = _ts(r["start_time"])
            onset[r["service"]] = min(onset.get(r["service"], t), t)
    log_err = defaultdict(int)
    for line in open(os.path.join(d, "logs.jsonl")):
        r = json.loads(line)
        if r["timestamp"] >= wstart and r.get("level") == "error":
            log_err[r["service"]] += 1
    return {
        "id": os.path.basename(d), "tl": tl, "services": services, "edges": edges,
        "mb": mb, "mw": mw, "mw_ts": mw_ts, "span_err": span_err, "log_err": log_err,
        "onset": onset,
    }


def transitive_callers(s, edges):
    """Services that call s directly or transitively (edges are caller->callee)."""
    callers_of = defaultdict(set)
    for caller, callee in edges:
        callers_of[callee].add(caller)
    seen, frontier = set(), [s]
    while frontier:
        cur = frontier.pop()
        for c in callers_of[cur]:
            if c not in seen:
                seen.add(c)
                frontier.append(c)
    return seen


def observed_vector(c, err_rate_cut, lat_mult):
    """Discretized observed presence-observables + observed onset time, per service."""
    mb, mw, span_err, log_err, onset = c["mb"], c["mw"], c["span_err"], c["log_err"], c["onset"]
    obs = {}
    for s in c["services"]:
        er_b = statistics.mean(mb[(s, "error_rate")]) if mb[(s, "error_rate")] else 0.0
        er_w = max(mw[(s, "error_rate")]) if mw[(s, "error_rate")] else 0.0
        lat_b = statistics.mean(mb[(s, "latency_ms")]) if mb[(s, "latency_ms")] else 0.0
        lat_w = max(mw[(s, "latency_ms")]) if mw[(s, "latency_ms")] else 0.0
        obs[s] = {
            "err_log": int(log_err.get(s, 0) > 0),
            "err_span": int(span_err.get(s, 0) > 0),
            "err_rate": int(er_w >= er_b + err_rate_cut),
            "lat": int(lat_b > 0 and lat_w >= lat_b * lat_mult),
            "onset": onset.get(s),  # float ts or None
        }
    return obs


def predicted_vector(s, mode, c):
    """Forward causal prediction for hypothesis (s, mode): affected = s ∪ transitive callers;
    each affected service shows the mode signal; predicted onset rank = hop distance from s."""
    callers = transitive_callers(s, c["edges"])
    affected = {s} | callers
    # hop distance from cause (cause=0), used as the predicted onset order
    dist = {s: 0}
    callers_of = defaultdict(set)
    for caller, callee in c["edges"]:
        callers_of[callee].add(caller)
    frontier = [(s, 0)]
    while frontier:
        cur, dd = frontier.pop()
        for caller in callers_of[cur]:
            if caller not in dist or dist[caller] > dd + 1:
                dist[caller] = dd + 1
                frontier.append((caller, dd + 1))
    pred = {}
    for v in c["services"]:
        on = v in affected
        pred[v] = {
            "err_log": int(on and mode == "error"),
            "err_span": int(on and mode == "error"),
            "err_rate": int(on and mode == "error"),
            "lat": int(on and mode == "latency"),
            "onset_rank": dist.get(v) if on else None,
        }
    return pred


def _observed_onset_ranks(obs, tol_s, jitter_s=0.0):
    """Bucket observed onset timestamps into ordinal ranks with tolerance `tol_s`
    (services within tol_s of each other tie). `jitter_s` perturbs deterministically to
    probe robustness. Returns {service: rank or None}."""
    import hashlib

    times = {}
    for s, o in obs.items():
        t = o["onset"]
        if t is None:
            continue
        if jitter_s:
            h = int(hashlib.md5(s.encode()).hexdigest(), 16) % 1000 / 1000.0
            t = t + (h - 0.5) * 2 * jitter_s
        times[s] = t
    if not times:
        return {s: None for s in obs}
    ordered = sorted(times.items(), key=lambda kv: kv[1])
    ranks, rank, last = {}, 0, None
    for s, t in ordered:
        if last is not None and (t - last) > tol_s:
            rank += 1
        ranks[s] = rank
        last = t
    return {s: ranks.get(s) for s in obs}


ERR_DIMS = ("err_log", "err_span", "err_rate")


def consistent(pred, obs, obs_onset_ranks, f_usable, use_onset_order):
    """Covering / hard-incompatibility consistency (Phase C spirit): a hypothesis is
    compatible with the observation iff it can **explain every observed anomaly** over
    F_usable and is itself anomalous. Concretely, for a hypothesis with affected set A and
    mode m: every service observed anomalous (in m's dims, over F_usable) must lie in A, the
    cause must itself be observed anomalous, and there must be no observed anomaly of the
    *other* mode that A does not cover. This rules a downstream symptom out as a cause — it
    cannot explain its own callee's anomaly — which is the causal content the exact-match
    signature lacked."""
    affected = {v for v in obs if pred[v]["onset_rank"] is not None}
    mode = "error" if any(pred[v]["err_span"] for v in affected) else "latency"

    def anom(v, dims):
        return any(obs[v][d] for d in dims if d in f_usable)

    err_anom = {v for v in obs if anom(v, ERR_DIMS)}
    lat_anom = {v for v in obs if anom(v, ("lat",))}
    mine, other = (err_anom, lat_anom) if mode == "error" else (lat_anom, err_anom)

    cause = min(affected, key=lambda v: pred[v]["onset_rank"])
    if not mine or cause not in mine:
        return False
    if not mine <= affected:            # must explain every observed anomaly of its mode
        return False
    if other - affected:                # and leave no other-mode anomaly unexplained
        return False
    if use_onset_order and "onset" in f_usable:
        # observed onset must not contradict predicted cause-first order among affected svcs
        for a in affected:
            for b in affected:
                if pred[a]["onset_rank"] < pred[b]["onset_rank"]:
                    ra, rb = obs_onset_ranks.get(a), obs_onset_ranks.get(b)
                    if ra is not None and rb is not None and ra > rb:
                        return False
    return True


def signature(pred, f_usable, use_onset_order):
    parts = []
    for v in sorted(pred):
        parts.append(tuple(pred[v][d] for d in DIMS if d in f_usable))
        if use_onset_order and "onset" in f_usable:
            parts.append(pred[v]["onset_rank"])
    return tuple(parts)


def resolve(c, f_usable, err_rate_cut, lat_mult, onset_tol_s, jitter_s=0.0):
    """Full structural inference for one case under a configuration. Returns the outcome
    and ground-truth retention (ground truth NOT used to build the outcome)."""
    obs = observed_vector(c, err_rate_cut, lat_mult)
    use_onset = "onset" in f_usable
    obs_onset_ranks = _observed_onset_ranks(obs, onset_tol_s, jitter_s) if use_onset else {}

    survivors = []  # (hyp, signature)
    for s in c["services"]:
        for mode in MODES:
            pred = predicted_vector(s, mode, c)
            if consistent(pred, obs, obs_onset_ranks, f_usable, use_onset):
                survivors.append(((s, mode), signature(pred, f_usable, use_onset)))

    classes = defaultdict(list)
    for hyp, sig in survivors:
        classes[sig].append(hyp)

    if not survivors:
        outcome = "NONE"
    elif len(classes) >= 2:
        outcome = "UNCERTAIN"
    elif len(survivors) == 1:
        outcome = "IDENTIFIED"
    else:
        outcome = "NON_IDENTIFIABLE"

    tl = c["tl"]
    true_mode = "latency" if tl["fault_type"] == "latency_only" else "error"
    true_hyp = (tl["root_cause"], true_mode)
    retained = any(h == true_hyp for h, _ in survivors)
    # correct resolution: IDENTIFIED and the unique survivor is the true hypothesis
    correct = outcome == "IDENTIFIED" and survivors and survivors[0][0] == true_hyp
    return {
        "outcome": outcome, "n_survivors": len(survivors), "n_classes": len(classes),
        "retained": retained, "correct": correct, "fault_type": tl["fault_type"],
    }


def main():
    cases = [load_case(d) for d in sorted(glob.glob(BASE + "/*")) if os.path.isdir(d)]
    if not cases:
        raise SystemExit(f"no cases under {BASE}; run scripts/gen_trace_localization_corpus.py first")
    print(f"corpus: {len(cases)} cases | hypotheses/case = 2×|services|\n")

    ER, LAT = 0.05, 2.0  # a central discretization; the factorial sweep is below
    TOL = 5.0            # onset bucket tolerance (s); the corpus onset gap is ~12s

    # 1. Availability sweep: outcome distribution and truth-retention per F_usable scenario.
    print("== availability (F_usable) sweep — outcome via partition semantics; truth only scores ==")
    print(f"   (err_rate_cut={ER}, lat_mult={LAT}, onset_tol={TOL}s)\n")
    hdr = f"   {'F_usable':13s} {'IDENT':>6} {'NON_ID':>7} {'UNCERT':>7} {'NONE':>5} {'retained':>9} {'correct':>8}"
    print(hdr)
    print("   " + "-" * (len(hdr) - 3))
    for name, fset in AVAIL_SCENARIOS.items():
        rows = [resolve(c, fset, ER, LAT, TOL) for c in cases]
        o = defaultdict(int)
        for r in rows:
            o[r["outcome"]] += 1
        ret = sum(r["retained"] for r in rows)
        cor = sum(r["correct"] for r in rows)
        print(f"   {name:13s} {o['IDENTIFIED']:>6} {o['NON_IDENTIFIABLE']:>7} {o['UNCERTAIN']:>7} "
              f"{o['NONE']:>5} {ret:>9}/{len(cases)} {cor:>6}/{len(cases)}")

    # 2. Onset robustness: tolerance / ties / jitter / absent onset (scenario 'all').
    print("\n== onset robustness (F_usable=all): correct-resolution rate ==")
    print(f"   {'onset_tol_s':>11} {'jitter_s':>9} {'IDENT':>6} {'UNCERT':>7} {'correct':>8}")
    for tol, jit in [(0.0, 0.0), (5.0, 0.0), (5.0, 3.0), (5.0, 8.0), (20.0, 0.0), (1e9, 0.0)]:
        rows = [resolve(c, AVAIL_SCENARIOS["all"], ER, LAT, tol, jit) for c in cases]
        o = defaultdict(int)
        for r in rows:
            o[r["outcome"]] += 1
        cor = sum(r["correct"] for r in rows)
        label_tol = "∞(no order)" if tol > 1e8 else f"{tol:g}"
        print(f"   {label_tol:>11} {jit:>9g} {o['IDENTIFIED']:>6} {o['UNCERTAIN']:>7} {cor:>6}/{len(cases)}")

    # 3. Factorial cutoff sweep (F_usable=all): stability of the outcome split.
    print("\n== factorial discretization sweep (F_usable=all, onset_tol=5s) ==")
    er_grid = [0.02, 0.05, 0.10, 0.20, 0.35]
    lat_grid = [1.2, 1.5, 2.0, 3.0]
    print("   IDENTIFIED count per (err_rate_cut × lat_mult):")
    print("   err\\lat " + " ".join(f"{lm:>5}" for lm in lat_grid))
    stable_cells = 0
    for er in er_grid:
        cells = []
        for lm in lat_grid:
            rows = [resolve(c, AVAIL_SCENARIOS["all"], er, lm, TOL) for c in cases]
            ident = sum(1 for r in rows if r["outcome"] == "IDENTIFIED")
            cells.append(ident)
            if ident >= len(cases) - 2:  # "stable" = ≥ n-2 identified
                stable_cells += 1
        print(f"   {er:<7} " + " ".join(f"{x:>5}" for x in cells))
    print(f"   stability criterion: IDENTIFIED ≥ {len(cases) - 2}/{len(cases)}; "
          f"stable in {stable_cells}/{len(er_grid) * len(lat_grid)} grid cells")


if __name__ == "__main__":
    main()
