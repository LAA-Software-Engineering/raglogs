#!/usr/bin/env python3
"""Calibrate + evaluate the abstention gate (#79), RCAEval-only.

Selects the frozen transform scales ``tau_log`` / ``tau_trace`` / ``tau_metric`` and
the abstention ``threshold`` on RCAEval, and reports **held-out** performance via
nested leave-one-system-out — so the recall-constrained operating point is not
overfit (per ``docs/design-abstention.md``). Uses the *product* transforms from
``src.core.rca.abstention`` (single source of truth) so the calibration measures
exactly what ships.

Per case, same width W as the spike, two labelled windows:

    incident (label 1): window [inject, inject+W],  baseline [inject-W, inject]
    healthy  (label 0): window [inject-W, inject],  baseline [inject-2W, inject-W]

Per window we keep the *raw* per-service, per-modality quantities (log incident /
baseline error rates, trace span-rate ratio, metric change) so a (tau) choice can be
scored without re-reading parquet. The window-anomaly score is
``max`` over available arms of the arm's ``max`` over services.

    python scripts/calibrate_abstention.py --suites re3          # extract + calibrate
    python scripts/calibrate_abstention.py --suites re3,re2 --recall-floor 0.99

Extraction is cached to ``--cache``; re-run calibration freely once cached.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from itertools import product
from pathlib import Path

from scripts.spike_multimodal_rca import _dl, _sec
from src.core.rca.abstention import (
    log_rate_anomaly,
    magnitude_anomaly,
    ratio_anomaly,
    window_anomaly,
)
from src.eval.rcaeval import _infer_level, parse_case_dir_name, parse_inject_time

REPO_ID = "phamquiluan/RCAEval"
CACHE = Path("/tmp/claude-1000/-home-leonardo-GitHub-raglogs/21cfcd59-4c37-42e9-abfd-55fe8b41ef92/scratchpad/abstention_cal.jsonl")
TAU_GRID = [0.25, 0.5, 1.0, 2.0, 4.0]


# ── extraction (raw per-service, per-modality quantities) ─────────────────────
def _log_rates(case, ws, we, bs, be):
    import pyarrow.parquet as pq

    try:
        t = pq.read_table(_dl(case, "logs.parquet"),
                          columns=["timestamp", "container_name", "message"]).to_pylist()
    except Exception:  # noqa: BLE001
        return {}
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


def _trace_ratios(case, ws, we, bs, be):
    import pyarrow.parquet as pq

    try:
        t = pq.read_table(_dl(case, "traces.parquet"),
                          columns=["serviceName", "startTimeMillis"]).to_pylist()
    except Exception:  # noqa: BLE001
        return None  # traces genuinely absent for this case
    win, base = Counter(), Counter()
    for r in t:
        sec, s = _sec(r["startTimeMillis"]), r["serviceName"]
        if not s or sec is None:
            continue
        if ws <= sec < we:
            win[s] += 1
        elif bs <= sec < be:
            base[s] += 1
    wdur, bdur = (we - ws) or 1, (be - bs) or 1
    return {s: (win.get(s, 0) / wdur + 1e-9) / (base.get(s, 0) / bdur + 1e-9)
            for s in set(win) | set(base)}


def _metric_changes(case, ws, we, bs, be):
    import statistics

    import pyarrow.parquet as pq

    try:
        t = pq.read_table(_dl(case, "metrics.parquet"))
    except Exception:  # noqa: BLE001
        return None
    cols = t.column_names
    tcol = "time" if "time" in cols else cols[0]
    times = [_sec(v) for v in t.column(tcol).to_pylist()]
    out: dict[str, float] = defaultdict(float)
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
            out[svc] = max(out[svc], abs(statistics.mean(w) - bm) / (abs(bm) + 1e-9))
    return dict(out)


def _window_row(case, system, label, ws, we, bs, be):
    logs = _log_rates(case, ws, we, bs, be)
    traces = _trace_ratios(case, ws, we, bs, be)
    metrics = _metric_changes(case, ws, we, bs, be)
    return {
        "case": case, "system": system, "label": label,
        "logs": logs or None,          # {svc: [inc_rate, base_rate]}
        "traces": traces or None,      # {svc: ratio}
        "metrics": metrics or None,    # {svc: change}
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
            print(f"  {case}")
        except Exception as e:  # noqa: BLE001
            print(f"  skip {case}: {type(e).__name__} {str(e)[:80]}")
    out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(f"wrote {len(rows)} window-rows -> {out}")
    return rows


# ── scoring ───────────────────────────────────────────────────────────────────
def _avail_pattern(row) -> str:
    return "".join(k for k, m in (("L", "logs"), ("T", "traces"), ("M", "metrics")) if row[m])


def _score(row, taus) -> float:
    tl, tt, tm = taus
    logs = traces = metrics = None
    if row["logs"]:
        logs = max(log_rate_anomaly(i, b, tl) for i, b in row["logs"].values())
    if row["traces"]:
        traces = max(ratio_anomaly(r, tt) for r in row["traces"].values())
    if row["metrics"]:
        metrics = max(magnitude_anomaly(c, tm) for c in row["metrics"].values())
    return window_anomaly(logs=logs, traces=traces, metrics=metrics).score


def _threshold_for_recall(pos_scores, floor):
    """Highest threshold whose incident recall >= floor (most abstention, still
    catching >= floor of incidents). Scores are compared with >= threshold = explain."""
    best = 0.0
    for cand in sorted(set(pos_scores)):
        recall = sum(1 for s in pos_scores if s >= cand) / len(pos_scores)
        if recall >= floor:
            best = cand
    return best


def _select(train, floor):
    """Grid-search taus; for each, threshold = recall-floor point; pick the taus
    maximising healthy abstention on train at that threshold."""
    best = None
    for taus in product(TAU_GRID, TAU_GRID, TAU_GRID):
        pos = [_score(r, taus) for r in train if r["label"] == 1]
        neg = [_score(r, taus) for r in train if r["label"] == 0]
        if not pos or not neg:
            continue
        thr = _threshold_for_recall(pos, floor)
        abstain = sum(1 for s in neg if s < thr) / len(neg)
        key = (abstain, -thr)  # tie-break toward a lower (safer) threshold
        if best is None or key > best[0]:
            best = (key, taus, thr)
    return best[1], best[2]  # taus, threshold


def calibrate(rows, floor):
    systems = sorted({r["system"] for r in rows})
    print(f"\n=== abstention calibration (nested LOSO, recall floor {floor:.0%}) ===")
    print(f"systems: {systems}  windows: {len(rows)}")

    ho_pos, ho_neg = defaultdict(list), defaultdict(list)  # per availability pattern
    overall = {"rec_n": 0, "rec_hit": 0, "ab_n": 0, "ab_hit": 0}
    for held in systems:
        train = [r for r in rows if r["system"] != held]
        test = [r for r in rows if r["system"] == held]
        taus, thr = _select(train, floor)
        for r in test:
            s = _score(r, taus)
            pat = _avail_pattern(r)
            if r["label"] == 1:
                overall["rec_n"] += 1
                overall["rec_hit"] += s >= thr
                ho_pos[pat].append(s >= thr)
            else:
                overall["ab_n"] += 1
                overall["ab_hit"] += s < thr
                ho_neg[pat].append(s < thr)
        print(f"  held-out {held}: taus={taus} thr={thr:.3f}")

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

    # Final frozen params: re-fit on ALL development data.
    taus, thr = _select(rows, floor)
    print(f"\nFROZEN defaults (re-fit on all dev data): tau_log/trace/metric={taus} threshold={thr:.3f}")
    print("  (report the HELD-OUT numbers above as the gate's performance, not an in-sample fit)")


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
