#!/usr/bin/env python3
"""SPIKE (#118, Phase C/D): a logs+traces+metrics feature table + a tiny learned
ranker, evaluated LEAVE-ONE-SYSTEM-OUT — before committing to src/core ingestion.

The single-modality spikes showed complementary, system-dependent signals (logs
win online-boutique self-contained faults; traces win train-ticket propagation
faults; sock-shop has only logs+metrics). The open question is *routing without a
label*: can a model fed all three modalities' features learn to pick the root
cause per case, and does it hold on a held-out system it never trained on?

Two phases (feature table is cached so the model is fast to iterate):

    python scripts/spike_multimodal_rca.py --extract   # build + cache features
    python scripts/spike_multimodal_rca.py --eval       # train + LOSO report

Per (case, candidate service) features, over incident [inject, inject+post] vs
baseline [inject-pre, inject]:
  log_err        error-level log lines
  log_err_group  largest (service,fingerprint) error group for the service
  log_stack      originating stack-trace lines
  tr_rate        trace span-rate ratio (incident/baseline); 0 if no traces
  tr_dur         trace p95-duration ratio; 0 if no traces
  met_anom       max per-metric change ratio for the service; 0 if no metrics
label = 1 if service is the injected root cause.

No src/core changes. sklearn is already a dependency.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from src.core.normalization.fingerprint import fingerprint_message
from src.eval.rcaeval import _infer_level, parse_case_dir_name, parse_inject_time

REPO_ID = "phamquiluan/RCAEval"
CACHE = Path("/tmp/claude-1000/-home-leonardo-GitHub-raglogs/21cfcd59-4c37-42e9-abfd-55fe8b41ef92/scratchpad/mm_features.jsonl")
PRE, POST = 300, 600

_STACK_RE = re.compile(
    r"\n\s*at\s+\S|\bat\s[\w.$]+\([\w.$ ]*:\d+\)|Traceback \(most recent call last\)"
    r'|\n\s*File "[^"]+", line \d+|\bpanic:\s|\bgoroutine\s+\d+\s+\[|Exception in thread'
    r"|nested exception is [\w.$]+",
    re.IGNORECASE,
)
_ERR_RE = re.compile(r"\b(error|exception|traceback|fatal|panic)", re.IGNORECASE)


def _p95(xs):
    if not xs:
        return 0.0
    return statistics.quantiles(xs, n=20)[18] if len(xs) >= 20 else float(max(xs))


def _dl(case, fn):
    from huggingface_hub import hf_hub_download

    return hf_hub_download(REPO_ID, repo_type="dataset", filename=f"{case}/{fn}")


def _sec(v):
    try:
        f = float(v)
        return f / 1000.0 if f > 1e12 else f
    except (TypeError, ValueError):
        return None


def _log_features(case, inject):
    import pyarrow.parquet as pq

    t = pq.read_table(_dl(case, "logs.parquet"), columns=["timestamp", "container_name", "message"]).to_pylist()
    err = Counter()
    grp = Counter()
    stack = Counter()
    for r in t:
        s = r["container_name"]
        sec = _sec(r["timestamp"])
        if not s or sec is None or not (inject <= sec <= inject + POST) or r["message"] is None:
            continue
        m = str(r["message"])
        if _infer_level(m) == "error":
            err[s] += 1
            grp[(s, fingerprint_message(m)[1])] += 1
        if _STACK_RE.search(m):
            stack[s] += 1
    grp_by_svc = defaultdict(int)
    for (s, _fp), c in grp.items():
        grp_by_svc[s] = max(grp_by_svc[s], c)
    return err, grp_by_svc, stack


def _trace_features(case, inject):
    import pyarrow.parquet as pq

    try:
        t = pq.read_table(_dl(case, "traces.parquet"), columns=["serviceName", "startTimeMillis", "duration"]).to_pylist()
    except Exception:  # noqa: BLE001
        return {}, {}
    bc, ic = Counter(), Counter()
    bd, idur = defaultdict(list), defaultdict(list)
    for r in t:
        sec = _sec(r["startTimeMillis"])
        s = r["serviceName"]
        if not s or sec is None:
            continue
        try:
            d = int(r["duration"])
        except (TypeError, ValueError):
            continue
        if inject - PRE <= sec < inject:
            bc[s] += 1
            bd[s].append(d)
        elif inject <= sec <= inject + POST:
            ic[s] += 1
            idur[s].append(d)
    rate, dur = {}, {}
    for s in ic:
        rate[s] = (ic[s] / POST + 1e-9) / (bc.get(s, 0) / PRE + 1e-9)
        dur[s] = (_p95(idur.get(s, [])) + 1) / (_p95(bd.get(s, [])) + 1)
    return rate, dur


def _metric_features(case, inject):
    import pyarrow.parquet as pq

    try:
        t = pq.read_table(_dl(case, "metrics.parquet"))
    except Exception:  # noqa: BLE001
        return {}
    cols = t.column_names
    tcol = "time" if "time" in cols else cols[0]
    times = [_sec(v) for v in t.column(tcol).to_pylist()]
    anom = defaultdict(float)
    for c in cols:
        if c == tcol or "_" not in c:
            continue
        svc = c.rsplit("_", 1)[0]
        vals = t.column(c).to_pylist()
        base, inc = [], []
        for tv, v in zip(times, vals):
            if tv is None or v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if inject - PRE <= tv < inject:
                base.append(fv)
            elif inject <= tv <= inject + POST:
                inc.append(fv)
        if base and inc:
            bm = statistics.mean(base) or 1e-9
            ratio = abs(statistics.mean(inc) - statistics.mean(base)) / (abs(bm) + 1e-9)
            anom[svc] = max(anom[svc], ratio)
    return dict(anom)


def _canon(s):
    return s.replace("-", "").replace("_", "").lower()


def extract(suites=("re3",), out: Path = CACHE):
    from huggingface_hub import HfApi

    files = HfApi().list_repo_files(REPO_ID, repo_type="dataset")
    prefixes = tuple(suites)
    cases = sorted({f.split("/")[0] for f in files if f.startswith(prefixes) and "/" in f})
    rows = []
    for case in cases:
        try:
            meta = parse_case_dir_name(case)
            inject = parse_inject_time(open(_dl(case, "inject_time.txt")).read()).timestamp()
        except Exception as e:  # noqa: BLE001
            print(f"  skip {case}: {e}")
            continue
        try:
            err, grp, stack = _log_features(case, inject)
            rate, dur = _trace_features(case, inject)
            met = _metric_features(case, inject)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {case}: {type(e).__name__} {str(e)[:80]}")
            continue
        # candidate services: union across modalities, canonicalized to match label
        svcs = set(err) | set(rate) | set(met)
        truth = _canon(meta.service)
        # modality presence at the CASE level: "0.0" for a feature is ambiguous
        # (no anomaly vs no data), so record presence explicitly (ChatGPT review).
        has_traces = int(bool(rate))
        has_metrics = int(bool(met))
        has_logs = int(bool(err))
        for s in svcs:
            rows.append({
                "case": case, "system": meta.system, "service": s,
                "label": int(_canon(s) == truth),
                "log_err": err.get(s, 0), "log_grp": grp.get(s, 0), "log_stack": stack.get(s, 0),
                "tr_rate": rate.get(s, 0.0), "tr_dur": dur.get(s, 0.0),
                "met_anom": met.get(s, 0.0),
                "has_logs": has_logs, "has_traces": has_traces, "has_metrics": has_metrics,
            })
        pos = sum(1 for r in rows if r["case"] == case and r["label"])
        print(f"  {case}: {len(svcs)} svcs, {pos} labelled-positive")
    Path(out).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(f"\nwrote {len(rows)} rows for {len({r['case'] for r in rows})} cases -> {out}")


LOG = ["log_err", "log_grp", "log_stack"]
TRACE = ["tr_rate", "tr_dur"]
METRIC = ["met_anom"]
PRESENCE = ["has_logs", "has_traces", "has_metrics"]

# Ablations: which feature groups the model sees. Tests whether the lift is
# real multi-modal signal vs a missingness shortcut (ChatGPT review point 2).
VARIANTS = {
    "logs-only": LOG,
    "logs+traces": LOG + TRACE,
    "logs+metrics": LOG + METRIC,
    "all (no presence)": LOG + TRACE + METRIC,
    "all + presence": LOG + TRACE + METRIC + PRESENCE,
}


def _loso(rows, by_case, systems, feats):
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier

    per_sys, hit_t, n_t = {}, 0, 0
    for held in systems:
        tr = [r for r in rows if r["system"] != held]
        model = GradientBoostingClassifier(random_state=0).fit(
            np.array([[r[f] for f in feats] for r in tr], dtype=float),
            np.array([r["label"] for r in tr]),
        )
        cases = [c for c, rs in by_case.items() if rs[0]["system"] == held]
        h = 0
        for c in cases:
            cr = by_case[c]
            X = np.array([[r[f] for f in feats] for r in cr], dtype=float)
            h += cr[int(np.argmax(model.predict_proba(X)[:, 1]))]["label"] == 1
        per_sys[held] = (h, len(cases))
        hit_t += h
        n_t += len(cases)
    return per_sys, hit_t, n_t


def evaluate():
    rows = [json.loads(li) for li in CACHE.read_text().splitlines() if li.strip()]
    by_case = defaultdict(list)
    for r in rows:
        by_case[r["case"]].append(r)
    systems = sorted({r["system"] for r in rows})

    # oracle ceiling: is the true service even a candidate?
    oracle = sum(any(r["label"] for r in rs) for rs in by_case.values())
    print(f"\n=== multi-modal ranker, leave-one-system-out (RE3, {len(by_case)} cases) ===")
    print(f"oracle ceiling (truth is a candidate): {oracle}/{len(by_case)} = {oracle/len(by_case):.1%}")
    print("logs volume baseline: 28.9%\n")
    print(f"{'variant':20s} {'overall':>8s}   per-system (" + " ".join(systems) + ")")
    for name, feats in VARIANTS.items():
        per_sys, h, n = _loso(rows, by_case, systems, feats)
        ps = "  ".join(f"{sy} {per_sys[sy][0]}/{per_sys[sy][1]}" for sy in systems)
        print(f"{name:20s} {h/n:7.1%}   {ps}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--suites", default="re3", help="comma-separated RCAEval suites, e.g. re2,re3")
    ap.add_argument("--out", type=Path, default=CACHE, help="feature table output path")
    args = ap.parse_args()
    if args.extract:
        extract(suites=tuple(s.strip() for s in args.suites.split(",") if s.strip()), out=args.out)
    if args.eval or not args.extract:
        evaluate()


if __name__ == "__main__":
    main()
