#!/usr/bin/env python3
"""Spike (#181): signature-equality sensitivity on the trace-localization corpus (#170).

Time-boxed investigation, **not production code** — it de-risks the structural
assumption behind #177 Phases D/E before the partitioner is built. It asks, on the
24-case synthetic trace corpus: over discretized *usable* observables, does the
categorical signature ``S_O(C) `` separate a true cause from its propagated symptom,
and how sensitive is that to (1) how many observables are usable and (2) the
discretization cutoffs? The two failure modes to expose are the degenerate regimes —
"everything collapses into one non-identifiable blob" and "every class is distinct so
everything is UNCERTAIN".

Run (after `python scripts/gen_trace_localization_corpus.py`):
    python scripts/spike_signature_sensitivity.py
"""
from __future__ import annotations

import glob
import json
import os
import statistics
from collections import defaultdict

import yaml

BASE = "data/eval-cases/trace-loc"
ERROR_STATUS = "2"  # OTel ERROR span status_code in this corpus (only 0/2 occur)

# Observable dimensions we could put in a signature. err_log / err_span are boolean
# presence; err_rate / lat are continuous deltas discretized by a cutoff; onset is an
# ordinal rank of first-degradation used only by the "+onset" signature policy.
DIMS = ("err_log", "err_span", "err_rate", "lat")


def _load(d):
    case = yaml.safe_load(open(os.path.join(d, "case.yaml")))
    tl = case["trace_localization"]
    wstart = case["window"]["start"]
    services = sorted({s for e in tl.get("edges", []) for s in e} | {tl["root_cause"]})

    mb, mw = defaultdict(list), defaultdict(list)
    mw_ts = defaultdict(list)  # (svc,metric) -> [(ts,value)] in window, for onset
    for line in open(os.path.join(d, "metrics.jsonl")):
        r = json.loads(line)
        key = (r["service"], r["metric"])
        if r["ts"] < wstart:
            mb[key].append(r["value"])
        else:
            mw[key].append(r["value"])
            mw_ts[key].append((r["ts"], r["value"]))

    span_err = defaultdict(int)
    log_err = defaultdict(int)
    err_onset = {}  # svc -> first in-window ts with an error signal (span or log)
    for line in open(os.path.join(d, "spans.jsonl")):
        r = json.loads(line)
        if r["start_time"] >= wstart and str(r.get("status_code")) == ERROR_STATUS:
            span_err[r["service"]] += 1
            err_onset.setdefault(r["service"], r["start_time"])
            err_onset[r["service"]] = min(err_onset[r["service"]], r["start_time"])
    for line in open(os.path.join(d, "logs.jsonl")):
        r = json.loads(line)
        if r["timestamp"] >= wstart and r.get("level") == "error":
            log_err[r["service"]] += 1
            t = r["timestamp"]
            err_onset[svc] = min(err_onset[svc], t) if (svc := r["service"]) in err_onset else t
    return tl, services, mb, mw, mw_ts, span_err, log_err, err_onset


def _delta(mb, mw, svc, metric):
    """(baseline_mean, window_peak) for a service/metric."""
    b = statistics.mean(mb[(svc, metric)]) if mb[(svc, metric)] else 0.0
    w = max(mw[(svc, metric)]) if mw[(svc, metric)] else 0.0
    return b, w


def _lat_onset(mb, mw_ts, svc, mult):
    """First in-window ts where latency exceeds mult× baseline (for latency_only onset)."""
    b = statistics.mean(mb[(svc, "latency_ms")]) if mb[(svc, "latency_ms")] else 0.0
    for ts, v in sorted(mw_ts[(svc, "latency_ms")]):
        if b > 0 and v >= b * mult:
            return ts
    return None


def observables(svc, ctx, err_rate_cut, lat_mult):
    """Discretized observable vector for one service under the given cutoffs."""
    _tl, _svcs, mb, mw, _mw_ts, span_err, log_err, _onset = ctx
    er_b, er_w = _delta(mb, mw, svc, "error_rate")
    lat_b, lat_w = _delta(mb, mw, svc, "latency_ms")
    return {
        "err_log": int(log_err.get(svc, 0) > 0),
        "err_span": int(span_err.get(svc, 0) > 0),
        "err_rate": int(er_w >= er_b + err_rate_cut),
        "lat": int(lat_b > 0 and lat_w >= lat_b * lat_mult),
    }


def onset_rank(svc, ctx, lat_mult):
    """Ordinal onset bucket: earliest error/latency degradation among services -> 0,1,2…
    Returns None if the service never degrades."""
    _tl, services, mb, _mw, mw_ts, _se, _le, err_onset = ctx
    t = err_onset.get(svc)
    lt = _lat_onset(mb, mw_ts, svc, lat_mult)
    cand = [x for x in (t, lt) if x is not None]
    return min(cand) if cand else None


def analyse_case(d, err_rate_cut, lat_mult, use_onset):
    ctx = _load(d)
    tl, services = ctx[0], ctx[1]
    rc = tl["root_cause"]
    symptoms = [s for s in tl.get("symptom_services", []) if s != rc]

    obs = {s: observables(s, ctx, err_rate_cut, lat_mult) for s in services}
    # F_usable = observable dims that vary across services in THIS case (available AND
    # discriminating). A flat dim carries no signal and is dropped.
    f_usable = tuple(dim for dim in DIMS if len({obs[s][dim] for s in services}) > 1)

    # onset ranks (only when the +onset policy is on), bucketed to a dense ordinal
    onset_bucket = {}
    if use_onset:
        raw = {s: onset_rank(s, ctx, lat_mult) for s in services}
        order = sorted({t for t in raw.values() if t is not None})
        rank_of = {t: i for i, t in enumerate(order)}
        onset_bucket = {s: (rank_of[raw[s]] if raw[s] is not None else -1) for s in services}

    def sig(s):
        base = tuple(obs[s][dim] for dim in f_usable)
        return base + ((onset_bucket[s],) if use_onset else ())

    anomalous = [s for s in services if any(obs[s][dim] for dim in DIMS)]
    classes = defaultdict(list)
    for s in anomalous:
        classes[sig(s)].append(s)

    cause_class = classes.get(sig(rc), [])
    # Outcome (spike operationalisation, tied to ground truth):
    #  IDENTIFIED       cause sits alone in its signature class (structurally isolated)
    #  NON_IDENTIFIABLE cause shares its class with another anomalous service (a symptom)
    #  UNCERTAIN        cause is isolated but ≥2 other distinct anomalous classes remain
    #                   (distinguishable candidates that ranking, not structure, must resolve)
    if len(cause_class) > 1:
        outcome = "NON_IDENTIFIABLE"
    elif len(classes) >= 3:
        outcome = "UNCERTAIN"
    else:
        outcome = "IDENTIFIED"

    cause_vs_symptom = None
    if symptoms:
        cause_vs_symptom = all(sig(rc) != sig(sy) for sy in symptoms)

    return {
        "id": os.path.basename(d),
        "fault_type": tl["fault_type"],
        "f_usable": f_usable,
        "n_usable": len(f_usable),
        "n_anomalous": len(anomalous),
        "partition": len(classes),
        "outcome": outcome,
        "cause_separable": cause_vs_symptom,
    }


def sweep():
    cases = sorted(d for d in glob.glob(BASE + "/*") if os.path.isdir(d))
    if not cases:
        raise SystemExit(f"no cases under {BASE}; run scripts/gen_trace_localization_corpus.py first")

    print(f"corpus: {len(cases)} cases\n")

    # 1. |F_usable| distribution (independent of the signature policy sweep).
    base = [analyse_case(d, 0.05, 2.0, use_onset=False) for d in cases]
    by_ft = defaultdict(list)
    for r in base:
        by_ft[r["fault_type"]].append(r["n_usable"])
    print("== |F_usable| by fault type (presence dims that vary across services) ==")
    for ft in sorted(by_ft):
        vals = by_ft[ft]
        print(f"  {ft:13s} n={len(vals)}  |F_usable| min={min(vals)} max={max(vals)} "
              f"mean={statistics.mean(vals):.1f}")
    allv = [r["n_usable"] for r in base]
    print(f"  {'ALL':13s} n={len(allv)}  |F_usable| min={min(allv)} max={max(allv)} "
          f"mean={statistics.mean(allv):.1f}\n")

    # 2. Partition size & outcome distribution vs cutoffs and signature policy.
    print("== partition |H/~_O| and outcome distribution vs cutoffs / policy ==")
    grid = [(0.05, 2.0), (0.20, 1.5), (0.02, 1.2), (0.35, 3.0)]
    for use_onset in (False, True):
        policy = "presence+onset" if use_onset else "presence-only"
        print(f"\n  --- signature policy: {policy} ---")
        for er_cut, lat_mult in grid:
            rows = [analyse_case(d, er_cut, lat_mult, use_onset) for d in cases]
            outc = defaultdict(int)
            for r in rows:
                outc[r["outcome"]] += 1
            parts = [r["partition"] for r in rows]
            sep = [r for r in rows if r["cause_separable"] is not None]
            sep_ok = sum(1 for r in sep if r["cause_separable"])
            print(f"   err_rate_cut={er_cut:<4} lat_mult={lat_mult:<4} | "
                  f"|H/~| mean={statistics.mean(parts):.1f} (min {min(parts)}, max {max(parts)}) | "
                  f"IDENT={outc['IDENTIFIED']:2d} NONID={outc['NON_IDENTIFIABLE']:2d} "
                  f"UNCERT={outc['UNCERTAIN']:2d} | cause>symptom sep {sep_ok}/{len(sep)}")

    # 3. Per-fault-type separability under the recommended policy.
    print("\n== cause-vs-symptom separability by fault type (presence-only vs +onset) ==")
    for use_onset in (False, True):
        rows = [analyse_case(d, 0.05, 2.0, use_onset) for d in cases]
        agg = defaultdict(lambda: [0, 0])
        for r in rows:
            if r["cause_separable"] is not None:
                agg[r["fault_type"]][0] += int(r["cause_separable"])
                agg[r["fault_type"]][1] += 1
        policy = "presence+onset" if use_onset else "presence-only"
        cells = "  ".join(f"{ft}={ok}/{n}" for ft, (ok, n) in sorted(agg.items()))
        print(f"  {policy:15s}: {cells}")


if __name__ == "__main__":
    sweep()
