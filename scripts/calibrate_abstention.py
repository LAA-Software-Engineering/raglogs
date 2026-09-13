#!/usr/bin/env python3
"""Calibrate + evaluate the abstention gate (#79, Gen-3.1), RCAEval-only.

Selects the frozen gate parameters — ``tau_log`` / ``tau_metric`` and the metric
detector's corroboration ``k`` / ``threshold`` (T), plus the abstention decision
``threshold`` — on RCAEval, and reports **held-out** performance via nested
leave-one-system-out so the recall-constrained operating point isn't overfit
(``docs/eval-abstention.md``). Uses the *product* arms from ``src.core.rca`` so the
calibration measures exactly what ships; the gate is **logs + metrics only** (traces
are a localisation signal, not a detector).

The metric arm is the hierarchical detector (``metric_semantics``): per metric a
stable log-space anomaly, per service a corroborated top-k. RCAEval's metrics are
gauge-like/untyped (level path), so per ``(service, metric)`` we only need the window
**mean** — reconstructed as two synthetic samples fed to the real ``metric_arm`` (no
logic duplication).

**Caveat — this exercises the detector's LEVEL path only.** RCAEval has no typed
counters, so the counter/histogram *rate* path is never hit here; ``tau_metric`` (and
``k`` / ``T`` / ``threshold``) are fit on level-magnitude effects. A rate ``q``
(increment/sec) and a level ``q`` (absolute value) differ in magnitude before
``log1p → saturate(·, tau_metric)``, so the rate path is **calibrated-by-proxy and
unvalidated** until the typed-OTel external re-validation (unblocked by #164).

    python scripts/calibrate_abstention.py --suites re3,re2 --recall-floor 0.99
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from types import SimpleNamespace

from scripts.spike_multimodal_rca import _dl, _sec
from src.core.rca.abstention import log_rate_anomaly, window_anomaly
from src.core.rca.features import metric_arm
from src.eval.rcaeval import _infer_level, parse_case_dir_name, parse_inject_time

REPO_ID = "phamquiluan/RCAEval"
CACHE = Path(__file__).resolve().parents[1] / "data" / "abstention_cal.jsonl"

TAU_GRID = [0.25, 0.5, 1.0, 2.0, 4.0]
K_GRID = [2, 3, 4]
T_GRID = [0.3, 0.5, 0.7]

# Canonical windows for the synthetic metric samples (only used to bucket base vs
# incident; the level path just means each window's values).
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_BS, _IS, _IE = _T0, _T0 + timedelta(seconds=300), _T0 + timedelta(seconds=600)


# ── extraction (raw per-window quantities) ────────────────────────────────────
def _log_rates(case, ws, we, bs, be):
    import pyarrow.parquet as pq

    try:
        t = pq.read_table(_dl(case, "logs.parquet"),
                          columns=["timestamp", "container_name", "message"]).to_pylist()
    except Exception:  # noqa: BLE001
        return {}
    from collections import Counter
    win, base = Counter(), Counter()
    for r in t:
        sec, s = _sec(r["timestamp"]), r["container_name"]
        if not s or sec is None:
            continue
        if _infer_level(r["message"] or "") not in ("error", "fatal", "critical"):
            continue
        if ws <= sec < we:
            win[s] += 1
        elif bs <= sec < be:
            base[s] += 1
    wdur, bdur = (we - ws) or 1, (be - bs) or 1
    return {s: [win.get(s, 0) / wdur, base.get(s, 0) / bdur] for s in set(win) | set(base)}


def _metric_means(case, ws, we, bs, be):
    """Per ``(service, metric-column)`` the mean value in the incident and baseline
    windows — enough for the detector's level path (RCAEval metrics are untyped)."""
    import pyarrow.parquet as pq

    try:
        t = pq.read_table(_dl(case, "metrics.parquet"))
    except Exception:  # noqa: BLE001
        return None
    cols = t.column_names
    tcol = "time" if "time" in cols else cols[0]
    times = [_sec(v) for v in t.column(tcol).to_pylist()]
    out: dict[str, dict[str, list]] = defaultdict(dict)
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
            out[svc][c] = [statistics.mean(w), statistics.mean(b)]
    return {s: cols for s, cols in out.items() if cols} or None


def _window_row(case, system, label, ws, we, bs, be):
    return {
        "case": case, "system": system, "label": label,
        "logs": _log_rates(case, ws, we, bs, be) or None,   # {svc: [inc_rate, base_rate]}
        "metrics": _metric_means(case, ws, we, bs, be),     # {svc: {col: [mean_inc, mean_base]}}
    }


def extract(suites, width, out: Path):
    from huggingface_hub import HfApi

    files = HfApi().list_repo_files(REPO_ID, repo_type="dataset")
    cases = sorted({f.split("/")[0] for f in files if f.startswith(tuple(suites)) and "/" in f})
    W = B = width
    rows = []
    for case in cases:
        try:
            meta = parse_case_dir_name(case)
            inj = parse_inject_time(open(_dl(case, "inject_time.txt")).read()).timestamp()
            rows.append(_window_row(case, meta.system, 1, inj, inj + W, inj - B, inj))
            rows.append(_window_row(case, meta.system, 0, inj - B, inj, inj - 2 * B, inj - B))
            print(f"  {case}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {case}: {type(e).__name__} {str(e)[:80]}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(f"wrote {len(rows)} window-rows -> {out}")
    return rows


# ── scoring (reuses the product arms; gate = logs + metrics) ──────────────────
def _metric_samples(metrics: dict) -> list:
    """Two synthetic samples per (service, metric) — the cached window means — so the
    real ``metric_arm`` (level path) reproduces the detector exactly. ``metric_type``
    is None → level path; RCAEval has no typed counters, so the rate path is not
    exercised by this calibration (see the module docstring caveat)."""
    out = []
    for svc, cols in metrics.items():
        for col, (mi, mb) in cols.items():
            out.append(SimpleNamespace(service=svc, metric=col, value=mb,
                                       ts=_BS + timedelta(seconds=1), metric_type=None))
            out.append(SimpleNamespace(service=svc, metric=col, value=mi,
                                       ts=_IS + timedelta(seconds=1), metric_type=None))
    return out


def _prepare(rows):
    for r in rows:
        r["_msamples"] = _metric_samples(r["metrics"]) if r["metrics"] else None


def _score(row, params) -> float:
    tau_log, tau_metric, k, t = params
    logs = metrics = None
    if row["logs"]:
        logs = max(log_rate_anomaly(i, b, tau_log) for i, b in row["logs"].values())
    if row.get("_msamples"):
        metrics = metric_arm(row["_msamples"], _BS, _IS, _IE,
                             tau_metric=tau_metric, k=k, corroboration_threshold=t)
    return window_anomaly(logs=logs, metrics=metrics).score


def _threshold_for_recall(pos_scores, floor):
    best = 0.0
    for cand in sorted(set(pos_scores)):
        if sum(1 for s in pos_scores if s >= cand) / len(pos_scores) >= floor:
            best = cand
    return best


def _select(train, floor):
    """Grid-search (tau_log, tau_metric, k, T); threshold = recall-floor point; pick
    the params maximising healthy abstention on train."""
    best = None
    for params in product(TAU_GRID, TAU_GRID, K_GRID, T_GRID):
        pos = [_score(r, params) for r in train if r["label"] == 1]
        neg = [_score(r, params) for r in train if r["label"] == 0]
        if not pos or not neg:
            continue
        thr = _threshold_for_recall(pos, floor)
        abstain = sum(1 for s in neg if s < thr) / len(neg)
        key = (abstain, -thr)
        if best is None or key > best[0]:
            best = (key, params, thr)
    return best[1], best[2]


def _avail_pattern(row) -> str:
    return "".join(c for c, m in (("L", "logs"), ("M", "metrics")) if row[m])


def calibrate(rows, floor):
    _prepare(rows)
    systems = sorted({r["system"] for r in rows})
    print(f"\n=== abstention calibration (nested LOSO, recall floor {floor:.0%}) ===")
    print(f"systems: {systems}  windows: {len(rows)}  gate: logs+metrics")

    ho_pos, ho_neg = defaultdict(list), defaultdict(list)
    overall = {"rec_n": 0, "rec_hit": 0, "ab_n": 0, "ab_hit": 0}
    for held in systems:
        train = [r for r in rows if r["system"] != held]
        test = [r for r in rows if r["system"] == held]
        params, thr = _select(train, floor)
        for r in test:
            s = _score(r, params)
            pat = _avail_pattern(r)
            if r["label"] == 1:
                overall["rec_n"] += 1
                overall["rec_hit"] += s >= thr
                ho_pos[pat].append(s >= thr)
            else:
                overall["ab_n"] += 1
                overall["ab_hit"] += s < thr
                ho_neg[pat].append(s < thr)
        print(f"  held-out {held}: params(tau_log,tau_metric,k,T)={params} thr={thr:.3f}")

    rec = overall["rec_hit"] / max(overall["rec_n"], 1)
    ab = overall["ab_hit"] / max(overall["ab_n"], 1)
    print(f"\nHELD-OUT incident recall  : {rec:.1%} ({overall['rec_hit']}/{overall['rec_n']})")
    print(f"HELD-OUT healthy abstention: {ab:.1%} ({overall['ab_hit']}/{overall['ab_n']})")
    print("per availability pattern (held-out):")
    for pat in sorted(set(ho_pos) | set(ho_neg)):
        p, n = ho_pos.get(pat, []), ho_neg.get(pat, [])
        pr = f"{sum(p)}/{len(p)}" if p else "-"
        nr = f"{sum(n)}/{len(n)}" if n else "-"
        print(f"  {pat:5s} recall {pr:>8s}  abstention {nr:>8s}")

    params, thr = _select(rows, floor)  # re-fit on all dev data -> shipped frozen values
    print("\nFROZEN defaults (re-fit on all dev data):")
    print(f"  tau_log={params[0]} tau_metric={params[1]} k={params[2]} "
          f"corroboration_threshold={params[3]} threshold={thr:.3f}")
    print("  (report the HELD-OUT numbers above as the gate's performance)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suites", default="re3")
    ap.add_argument("--width", type=int, default=300)
    ap.add_argument("--recall-floor", type=float, default=0.99)
    ap.add_argument("--cache", type=Path, default=CACHE)
    ap.add_argument("--use-cache", action="store_true", help="skip extraction, read --cache")
    args = ap.parse_args()
    suites = tuple(s.strip() for s in args.suites.split(",") if s.strip())
    if args.use_cache and args.cache.exists():
        rows = [json.loads(x) for x in args.cache.read_text().splitlines() if x.strip()]
    else:
        rows = extract(suites, args.width, args.cache)
    calibrate(rows, args.recall_floor)


if __name__ == "__main__":
    main()
