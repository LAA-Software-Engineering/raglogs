#!/usr/bin/env python3
"""Spike (#181): signature-equality sensitivity before Phase D (#177), v3.

Time-boxed investigation, **not production code**. Prototypes the structural inference Phase D/E
require and measures whether categorical equality of *causal-hypothesis prediction* signatures over
*usable* observables can carry the IDENTIFIED / NON_IDENTIFIABLE / UNCERTAIN outcome — and how
sensitive that is to which observables are usable and to discretization.

Two earlier rounds (PR #195) were rejected for real architectural reasons; this version fixes them:
  - **Silent causes stay representable** (round-2 hard-rejected them). Consistency is *hard
    incompatibility only* — a hypothesis is eliminated only by a HARD prediction that the usable
    observation contradicts, never by "the cause emitted no local signal". A silent callee survives
    on its predicted *downstream* effects.
  - **NON_IDENTIFIABLE is reachable** (round-2 made it impossible). The signature is the per-
    coordinate *observable expectation* (present/absent) over usable (service, observable)
    coordinates, so two different hypotheses share a class exactly when the coordinate that would
    separate them is unavailable.
  - **Per-coordinate prediction**, not affected-set membership: an error hypothesis predicts
    ABSENT_HARD latency on its affected services, so a latency anomaly there is a real contradiction.
  - **Onset is a source-gated coordinate** with one discretization applied to *both* prediction and
    observation; disabling order removes the coordinate from the signature and the check.

Model (prototype for the still-open Phases A/C):
  hypotheses H = {(service, mode) : mode ∈ {error, error_silent, latency}}; a fault propagates UP the
  caller chain (affected = s ∪ transitive callers), cause-first. `error_silent` = a cause that emits
  only an ERROR *span* (no error log, no error-rate spike) — the partial-observability case. Ground
  truth (root_cause + fault-type→mode) only *scores* retention/correctness, never builds the label.

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
MODES = ("error", "error_silent", "latency")
PRESENCE = ("err_log", "err_span", "err_rate", "lat")

# prediction strengths
PRESENT_HARD, PRESENT_SOFT, ABSENT_HARD = "P!", "P?", "A!"

# availability scenarios: which observable *sources* were measured. onset_error is derived from
# ERROR spans (needs spans), onset_lat from latency metrics (needs metrics).
AVAIL = {
    "all": {"err_log", "err_span", "err_rate", "lat", "onset_error", "onset_lat"},
    "no_onset": {"err_log", "err_span", "err_rate", "lat"},
    "metrics_only": {"err_rate", "lat", "onset_lat"},
    "spans_only": {"err_span", "onset_error"},
    "logs_only": {"err_log"},
}


def _ts(s):
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
        k = (r["service"], r["metric"])
        (mb if r["ts"] < wstart else mw)[k].append(r["value"])
        if r["ts"] >= wstart:
            mw_ts[k].append((r["ts"], r["value"]))
    span_err, span_err_ts = defaultdict(int), {}
    for line in open(os.path.join(d, "spans.jsonl")):
        r = json.loads(line)
        if r["start_time"] >= wstart and str(r.get("status_code")) == ERROR_STATUS:
            span_err[r["service"]] += 1
            t = _ts(r["start_time"])
            span_err_ts[r["service"]] = min(span_err_ts.get(r["service"], t), t)
    log_err = defaultdict(int)
    for line in open(os.path.join(d, "logs.jsonl")):
        r = json.loads(line)
        if r["timestamp"] >= wstart and r.get("level") == "error":
            log_err[r["service"]] += 1
    return {"id": os.path.basename(d), "tl": tl, "services": services, "edges": edges,
            "mb": mb, "mw": mw, "mw_ts": mw_ts, "span_err": span_err,
            "span_err_ts": span_err_ts, "log_err": log_err}


def transitive_callers(s, edges):
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
    return seen, callers_of


def hop_dist(s, callers_of, services):
    dist = {s: 0}
    frontier = [(s, 0)]
    while frontier:
        cur, dd = frontier.pop()
        for caller in callers_of[cur]:
            if caller not in dist or dist[caller] > dd + 1:
                dist[caller] = dd + 1
                frontier.append((caller, dd + 1))
    return dist


def observed(c, err_rate_cut, lat_mult):
    """Per-service observed presence + onset times (error-onset from spans, lat-onset from metrics)."""
    o = {}
    for s in c["services"]:
        er_b = statistics.mean(c["mb"][(s, "error_rate")]) if c["mb"][(s, "error_rate")] else 0.0
        er_w = max(c["mw"][(s, "error_rate")]) if c["mw"][(s, "error_rate")] else 0.0
        lat_b = statistics.mean(c["mb"][(s, "latency_ms")]) if c["mb"][(s, "latency_ms")] else 0.0
        lat_w = max(c["mw"][(s, "latency_ms")]) if c["mw"][(s, "latency_ms")] else 0.0
        lat_onset = None
        for ts, v in sorted(c["mw_ts"][(s, "latency_ms")]):
            if lat_b > 0 and v >= lat_b * lat_mult:
                lat_onset = _ts(ts)
                break
        o[s] = {
            "err_log": int(c["log_err"].get(s, 0) > 0),
            "err_span": int(c["span_err"].get(s, 0) > 0),
            "err_rate": int(er_w >= er_b + err_rate_cut),
            "lat": int(lat_b > 0 and lat_w >= lat_b * lat_mult),
            "onset_error": c["span_err_ts"].get(s),
            "onset_lat": lat_onset,
        }
    return o


def predict(s, mode, c):
    """Per-(service, presence-observable) predicted strength, + predicted onset hop-rank per service."""
    affected, callers_of = transitive_callers(s, c["edges"])
    affected = {s} | affected
    dist = hop_dist(s, callers_of, c["services"])
    err_mode = mode in ("error", "error_silent")
    pred = {}
    for v in c["services"]:
        if v not in affected:
            cell = {d: ABSENT_HARD for d in PRESENCE}
            pred[v] = {"cell": cell, "onset": None}
            continue
        if v == s:
            if mode == "error":
                cell = {"err_log": PRESENT_HARD, "err_span": PRESENT_HARD,
                        "err_rate": PRESENT_HARD, "lat": ABSENT_HARD}
            elif mode == "error_silent":  # emits ONLY a trace ERROR span locally
                cell = {"err_log": ABSENT_HARD, "err_span": PRESENT_HARD,
                        "err_rate": ABSENT_HARD, "lat": ABSENT_HARD}
            else:  # latency
                cell = {"err_log": ABSENT_HARD, "err_span": ABSENT_HARD,
                        "err_rate": ABSENT_HARD, "lat": PRESENT_HARD}
        else:  # affected downstream caller: MAY surface the propagated symptom (soft)
            if err_mode:
                cell = {"err_log": PRESENT_SOFT, "err_span": PRESENT_SOFT,
                        "err_rate": PRESENT_SOFT, "lat": ABSENT_HARD}
            else:
                cell = {"err_log": ABSENT_HARD, "err_span": ABSENT_HARD,
                        "err_rate": ABSENT_HARD, "lat": PRESENT_SOFT}
        pred[v] = {"cell": cell, "onset": dist.get(v)}
    return pred, err_mode


def _avail_presence(fset):
    return tuple(d for d in PRESENCE if d in fset)


def mode_onset_source(mode, fset):
    """Onset source for a hypothesis mode: error/error_silent use error-onset (spans),
    latency uses latency-onset (metrics). None if that source is not usable. Mode-aware so
    F_usable=all keeps latency onset for latency hypotheses (not just error-onset globally)."""
    src = "onset_error" if mode in ("error", "error_silent") else "onset_lat"
    return src if src in fset else None


def _observed_onset_ranks(o, source, tol_s, jitter_s=0.0):
    import hashlib
    times = {}
    for s, cell in o.items():
        t = cell[source]
        if t is None:
            continue
        if jitter_s:
            h = int(hashlib.md5(s.encode()).hexdigest(), 16) % 1000 / 1000.0
            t += (h - 0.5) * 2 * jitter_s
        times[s] = t
    if not times:
        return {}
    ordered = sorted(times.items(), key=lambda kv: kv[1])
    ranks, rank, last = {}, 0, None
    for s, t in ordered:
        if last is not None and (t - last) > tol_s:
            rank += 1
        ranks[s] = rank
        last = t
    return ranks


def consistent(pred, o, fset, obs_onset_ranks, onset_usable):
    """Survive iff no HARD prediction is contradicted by a usable observation. Observed onset
    (from the mode's source) is used ONLY here, as a hard order check — never in the signature."""
    for v in o:
        cell = pred[v]["cell"]
        for d in _avail_presence(fset):
            p, obs = cell[d], o[v][d]
            if p == PRESENT_HARD and obs == 0:
                return False
            if p == ABSENT_HARD and obs == 1:
                return False
    if onset_usable:
        pred_on = {v: pred[v]["onset"] for v in o if pred[v]["onset"] is not None}
        for a in pred_on:
            for b in pred_on:
                if pred_on[a] < pred_on[b]:
                    ra, rb = obs_onset_ranks.get(a), obs_onset_ranks.get(b)
                    if ra is not None and rb is not None and ra > rb:
                        return False
    return True


def hyp_signature(pred, fset, onset_usable):
    """S_O(C): built PURELY from the hypothesis prediction over usable coordinates — presence
    expectation per (service, presence-observable), plus the predicted categorical onset order
    (hop-rank per affected service) when the mode's onset source is usable. No observed value
    enters here, so two hypotheses share a ~_O class iff their predictions agree on every usable
    coordinate (i.e. only their unavailable distinguishers differ)."""
    parts = []
    for v in sorted(pred):
        row = tuple(0 if pred[v]["cell"][d] == ABSENT_HARD else 1 for d in _avail_presence(fset))
        parts.append(row)
    if onset_usable:
        parts.append(tuple((v, pred[v]["onset"]) for v in sorted(pred)
                           if pred[v]["onset"] is not None))
    return tuple(parts)


def _all_coords_differ(pred_a, pred_b, services):
    """Coordinates (service, observable) where two hypothesis predictions differ — the
    distinguishers between them (over ALL coordinates, usable or not)."""
    diff = set()
    for v in services:
        for d in PRESENCE:
            ea = 0 if pred_a[v]["cell"][d] == ABSENT_HARD else 1
            eb = 0 if pred_b[v]["cell"][d] == ABSENT_HARD else 1
            if ea != eb:
                diff.add((v, d))
        if pred_a[v]["onset"] != pred_b[v]["onset"]:
            diff.add((v, "onset"))
    return diff


def _usable_coord(coord, fset):
    v, d = coord
    if d == "onset":
        return ("onset_error" in fset) or ("onset_lat" in fset)
    return d in fset


def resolve(c, fset, err_rate_cut, lat_mult, onset_tol_s, jitter_s=0.0):
    o = observed(c, err_rate_cut, lat_mult)
    # observed onset ranks per source (used only for hard consistency, never in the signature)
    ranks_by_src = {
        src: _observed_onset_ranks(o, src, onset_tol_s, jitter_s)
        for src in ("onset_error", "onset_lat") if src in fset
    }
    preds = {}
    survivors = []
    for s in c["services"]:
        for mode in MODES:
            pred, _ = predict(s, mode, c)
            preds[(s, mode)] = pred
            src = mode_onset_source(mode, fset)
            onset_usable = src is not None
            obs_ranks = ranks_by_src.get(src, {})
            if consistent(pred, o, fset, obs_ranks, onset_usable):
                survivors.append(((s, mode), hyp_signature(pred, fset, onset_usable)))
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
    true_mode = "latency" if tl["fault_type"] == "latency_only" else (
        "error_silent" if tl["fault_type"] == "symptom_only" else "error")
    true_hyp = (tl["root_cause"], true_mode)  # EXACT (service, mode)
    survivor_hyps = {h for h, _ in survivors}
    retained = true_hyp in survivor_hyps

    # correctness against #177's success criteria, exact hypothesis identity:
    #   IDENTIFIED  correct iff the sole survivor IS the true hypothesis.
    #   NON_IDENTIFIABLE correct iff the (single) surviving class contains the true hypothesis AND
    #     every co-member is separable-in-principle from it only by UNAVAILABLE distinguishers
    #     (anti-gaming: no co-member that a usable coordinate would have split off). D_missing =
    #     the union of those unavailable distinguishers, and must be non-empty.
    #   UNCERTAIN / NONE: not correct.
    correct = False
    d_missing = set()
    if outcome == "IDENTIFIED":
        correct = survivors[0][0] == true_hyp
    elif outcome == "NON_IDENTIFIABLE" and retained:
        cls = next(iter(classes.values()))
        legit = True
        for h in cls:
            if h == true_hyp:
                continue
            diff = _all_coords_differ(preds[true_hyp], preds[h], c["services"])
            if any(_usable_coord(cd, fset) for cd in diff):
                legit = False  # a usable coordinate distinguishes them -> should not co-class
                break
            d_missing |= diff
        correct = legit and bool(d_missing)
    return {"outcome": outcome, "n_survivors": len(survivors), "n_classes": len(classes),
            "retained": retained, "correct": correct, "d_missing": len(d_missing),
            "fault_type": tl["fault_type"], "true_mode": true_mode}


def main():
    cases = [load_case(d) for d in sorted(glob.glob(BASE + "/*")) if os.path.isdir(d)]
    if not cases:
        raise SystemExit(f"no cases under {BASE}; run scripts/gen_trace_localization_corpus.py first")
    print(f"corpus: {len(cases)} cases | hypotheses/case = 3×|services|\n")
    ER, LAT, TOL = 0.05, 2.0, 5.0

    print("== availability (F_usable) sweep — outcome from partition; truth only scores ==")
    print(f"   (err_rate_cut={ER}, lat_mult={LAT}, onset_tol={TOL}s)\n")
    hdr = (f"   {'F_usable':13s} {'IDENT':>6}{'NON_ID':>7}{'UNCERT':>7}{'NONE':>6}"
           f"{'retained':>10}{'correct':>9}")
    print(hdr + "\n   " + "-" * (len(hdr) - 3))
    for name, fset in AVAIL.items():
        rows = [resolve(c, fset, ER, LAT, TOL) for c in cases]
        oc = defaultdict(int)
        for r in rows:
            oc[r["outcome"]] += 1
        ret = sum(r["retained"] for r in rows)
        cor = sum(r["correct"] for r in rows)
        print(f"   {name:13s} {oc['IDENTIFIED']:>6}{oc['NON_IDENTIFIABLE']:>7}{oc['UNCERTAIN']:>7}"
              f"{oc['NONE']:>6}{ret:>8}/{len(cases)}{cor:>7}/{len(cases)}")

    # Explicit verification of the two properties the review demanded.
    print("\n== review-property checks (symptom_only = silent callee cause; exact (svc,mode)) ==")
    sym = [c for c in cases if c["tl"]["fault_type"] == "symptom_only"]
    rl = [resolve(c, AVAIL["logs_only"], ER, LAT, TOL) for c in sym]
    rs = [resolve(c, AVAIL["spans_only"], ER, LAT, TOL) for c in sym]
    ra = [resolve(c, AVAIL["all"], ER, LAT, TOL) for c in sym]
    print(f"   logs_only : true (svc,error_silent) retained {sum(r['retained'] for r in rl)}/{len(sym)}"
          f", NON_IDENTIFIABLE {sum(r['outcome'] == 'NON_IDENTIFIABLE' for r in rl)}/{len(sym)}"
          f" (cause ≡ loud caller; D_missing avg {statistics.mean([r['d_missing'] for r in rl]):.1f} coords)")
    print(f"   spans_only: NON_IDENTIFIABLE {sum(r['outcome'] == 'NON_IDENTIFIABLE' for r in rs)}/{len(sym)}"
          f" (span present, but err_log/err_rate absent → error vs error_silent MODE unresolved),"
          f" legit/retained {sum(r['correct'] for r in rs)}/{len(sym)}")
    print(f"   all       : IDENTIFIED {sum(r['outcome'] == 'IDENTIFIED' for r in ra)}/{len(sym)}"
          f", exact-correct {sum(r['correct'] for r in ra)}/{len(sym)}"
          f" (every distinguisher usable → unique (svc,mode))")

    print("\n== onset robustness (F_usable=all): outcome + correct ==")
    print(f"   {'onset_tol_s':>11}{'jitter_s':>9}{'IDENT':>6}{'NON_ID':>7}{'UNCERT':>7}{'correct':>9}")
    for tol, jit, disable in [(0.0, 0.0, False), (5.0, 0.0, False), (5.0, 8.0, False),
                              (20.0, 0.0, False), (0.0, 0.0, True)]:
        fset = AVAIL["no_onset"] if disable else AVAIL["all"]
        rows = [resolve(c, fset, ER, LAT, tol, jit) for c in cases]
        oc = defaultdict(int)
        for r in rows:
            oc[r["outcome"]] += 1
        cor = sum(r["correct"] for r in rows)
        label = "order-off" if disable else f"{tol:g}"
        print(f"   {label:>11}{jit:>9g}{oc['IDENTIFIED']:>6}{oc['NON_IDENTIFIABLE']:>7}"
              f"{oc['UNCERTAIN']:>7}{cor:>7}/{len(cases)}")

    print("\n== factorial discretization sweep (F_usable=all, onset_tol=5s): correct/24 ==")
    er_grid, lat_grid = [0.02, 0.05, 0.10, 0.20, 0.35], [1.2, 1.5, 2.0, 3.0]
    print("   err\\lat " + " ".join(f"{lm:>5}" for lm in lat_grid))
    stable = 0
    for er in er_grid:
        cells = []
        for lm in lat_grid:
            cor = sum(resolve(c, AVAIL["all"], er, lm, TOL)["correct"] for c in cases)
            cells.append(cor)
            if cor >= len(cases) - 2:
                stable += 1
        print(f"   {er:<7} " + " ".join(f"{x:>5}" for x in cells))
    print(f"   stability (correct ≥ {len(cases) - 2}/{len(cases)}): "
          f"{stable}/{len(er_grid) * len(lat_grid)} cells")


if __name__ == "__main__":
    main()
