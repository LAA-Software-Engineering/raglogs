#!/usr/bin/env python3
"""Abstention spike (#118 gen-2 / #79 follow-up). RCAEval-only, no src/core change.

The frozen OTel-Demo run (#79) abstained on 0/2 healthy windows: raglogs invents
an incident from healthy background traffic. Today it abstains only when there are
*no* clusters at all (`summarizer.py`). This spike measures whether a **window-level
anomaly score** — "is anything actually elevated vs the baseline, in any modality?"
— separates real incidents from healthy windows, using **pre-injection windows as
healthy negatives** (RCAEval has 720-900s of pre-fault data per case, so a healthy
window with its own healthy baseline fits entirely before the injection).

Per case, same width W and baseline B:

  incident (positive): window [inject, inject+W],  baseline [inject-B, inject]
  healthy  (negative): window [inject-B, inject],  baseline [inject-2B, inject-B]

Window-anomaly score = max over services of the per-service anomaly
  max( log-error-rate elevation, trace-rate deviation, metric mean-change ),
i.e. the strongest single-service, single-modality deviation in the window. A good
abstention rule is a threshold on this score. We report the score distributions,
ROC-AUC, and abstention accuracy at the break-even threshold.

    python scripts/spike_abstention_rca.py --suites re3
"""
from __future__ import annotations

import argparse
import statistics
from collections import Counter, defaultdict

# Reuse the parquet IO + parsing from the multi-modal spike (import-safe: it only
# runs on __main__).
from scripts.spike_multimodal_rca import _dl, _sec
from src.eval.rcaeval import _infer_level, parse_case_dir_name, parse_inject_time

REPO_ID = "phamquiluan/RCAEval"


def _err_rates(case, ws, we, bs, be):
    """Per service: (window error-rate) / (baseline error-rate), from logs.parquet."""
    import pyarrow.parquet as pq

    try:
        t = pq.read_table(
            _dl(case, "logs.parquet"), columns=["timestamp", "container_name", "message"]
        ).to_pylist()
    except Exception:  # noqa: BLE001
        return {}
    win: Counter = Counter()
    base: Counter = Counter()
    for r in t:
        sec = _sec(r["timestamp"])
        s = r["container_name"]
        if not s or sec is None:
            continue
        lvl = _infer_level(r["message"] or "")
        if lvl not in ("error", "fatal", "critical"):
            continue
        if ws <= sec < we:
            win[s] += 1
        elif bs <= sec < be:
            base[s] += 1
    wdur, bdur = (we - ws) or 1, (be - bs) or 1
    out = {}
    for s in set(win) | set(base):
        wr, br = win[s] / wdur, base[s] / bdur
        out[s] = (wr + 1e-9) / (br + 1e-9)
    return out


def _rate_dev(case, ws, we, bs, be):
    """Per service: span-rate window/baseline ratio, from traces.parquet."""
    import pyarrow.parquet as pq

    try:
        t = pq.read_table(
            _dl(case, "traces.parquet"), columns=["serviceName", "startTimeMillis"]
        ).to_pylist()
    except Exception:  # noqa: BLE001
        return {}
    win: Counter = Counter()
    base: Counter = Counter()
    for r in t:
        sec = _sec(r["startTimeMillis"])
        s = r["serviceName"]
        if not s or sec is None:
            continue
        if ws <= sec < we:
            win[s] += 1
        elif bs <= sec < be:
            base[s] += 1
    wdur, bdur = (we - ws) or 1, (be - bs) or 1
    out = {}
    for s in set(win) | set(base):
        wr, br = win[s] / wdur, base[s] / bdur
        out[s] = (wr + 1e-9) / (br + 1e-9)
    return out


def _met_change(case, ws, we, bs, be):
    """Per service: max metric mean-change ratio window vs baseline."""
    import pyarrow.parquet as pq

    try:
        t = pq.read_table(_dl(case, "metrics.parquet"))
    except Exception:  # noqa: BLE001
        return {}
    cols = t.column_names
    tcol = "time" if "time" in cols else cols[0]
    times = [_sec(v) for v in t.column(tcol).to_pylist()]
    anom: dict[str, float] = defaultdict(float)
    for c in cols:
        if c == tcol or "_" not in c:
            continue
        svc = c.rsplit("_", 1)[0]
        vals = t.column(c).to_pylist()
        w, b = [], []
        for tv, v in zip(times, vals):
            if tv is None or v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if ws <= tv < we:
                w.append(fv)
            elif bs <= tv < be:
                b.append(fv)
        if w and b:
            bm = statistics.mean(b)
            anom[svc] = max(anom[svc], abs(statistics.mean(w) - bm) / (abs(bm) + 1e-9))
    return dict(anom)


def _window_score(case, ws, we, bs, be):
    """Max over services of the strongest single-modality deviation in the window.

    Logs/traces are *ratios* (>1 = elevated); we use max(ratio-1, 0) so a quiet
    service contributes 0. Metrics are already change ratios. The window score is
    the single most-anomalous (service, modality) — a real incident has at least
    one; a healthy window should have none."""
    err = _err_rates(case, ws, we, bs, be)
    rate = _rate_dev(case, ws, we, bs, be)
    met = _met_change(case, ws, we, bs, be)
    best = 0.0
    for s in set(err) | set(rate) | set(met):
        dev = max(
            max(err.get(s, 1.0) - 1.0, 0.0),
            max(rate.get(s, 1.0) - 1.0, 0.0),
            met.get(s, 0.0),
        )
        best = max(best, dev)
    return best


def _auc(pos, neg):
    """ROC-AUC via the Mann-Whitney U statistic (P(pos score > neg score))."""
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(pos) * len(neg))


def run(suites=("re3",), width=300):
    from huggingface_hub import HfApi

    files = HfApi().list_repo_files(REPO_ID, repo_type="dataset")
    cases = sorted({f.split("/")[0] for f in files if f.startswith(tuple(suites)) and "/" in f})
    W = B = width
    pos_scores, neg_scores, per_sys = [], [], defaultdict(lambda: ([], []))
    for case in cases:
        try:
            meta = parse_case_dir_name(case)
            inject = parse_inject_time(open(_dl(case, "inject_time.txt")).read()).timestamp()
            incident = _window_score(case, inject, inject + W, inject - B, inject)
            healthy = _window_score(case, inject - B, inject, inject - 2 * B, inject - B)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {case}: {type(e).__name__} {str(e)[:80]}")
            continue
        pos_scores.append(incident)
        neg_scores.append(healthy)
        per_sys[meta.system][0].append(incident)
        per_sys[meta.system][1].append(healthy)
        print(f"  {case:40s} incident={incident:8.2f}  healthy={healthy:8.2f}")

    print(f"\n=== abstention window-anomaly separability (suites={','.join(suites)}, W={W}s) ===")
    print(f"cases: {len(pos_scores)} incident / {len(neg_scores)} healthy (pre-injection)")
    print(f"incident score: median {statistics.median(pos_scores):.2f}  mean {statistics.mean(pos_scores):.2f}")
    print(f"healthy  score: median {statistics.median(neg_scores):.2f}  mean {statistics.mean(neg_scores):.2f}")
    print(f"ROC-AUC (incident>healthy): {_auc(pos_scores, neg_scores):.3f}")

    # Break-even threshold sweep: pick the threshold maximising balanced accuracy.
    cand = sorted(set(pos_scores + neg_scores))
    best_t, best_bacc = 0.0, 0.0
    for t in cand:
        tp = sum(1 for p in pos_scores if p >= t)
        tn = sum(1 for n in neg_scores if n < t)
        bacc = 0.5 * (tp / len(pos_scores) + tn / len(neg_scores))
        if bacc > best_bacc:
            best_bacc, best_t = bacc, t
    tp = sum(1 for p in pos_scores if p >= best_t)
    tn = sum(1 for n in neg_scores if n < best_t)
    print(f"best threshold {best_t:.2f}: balanced-acc {best_bacc:.1%} "
          f"(explain {tp}/{len(pos_scores)} incidents, abstain {tn}/{len(neg_scores)} healthy)")
    print("\nper system (ROC-AUC):")
    for sy, (p, n) in sorted(per_sys.items()):
        print(f"  {sy:8s} AUC {_auc(p, n):.3f}  (n={len(p)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suites", default="re3", help="comma-separated RCAEval suites")
    ap.add_argument("--width", type=int, default=300, help="window/baseline width in seconds")
    args = ap.parse_args()
    run(tuple(s.strip() for s in args.suites.split(",") if s.strip()), width=args.width)


if __name__ == "__main__":
    main()
