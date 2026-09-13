#!/usr/bin/env python3
"""Leave-one-*-out evaluation of the trace-graph propagation reranker (#118 / #79).

The narrow question (user-directed, post-abstention-shelve): *when the symptom
appears downstream, can trace topology move the true upstream culprit above the
loud caller/symptom service?* This measures exactly that — top-1 with the learned
ranker alone vs. the ranker **followed by** the propagation reranker
(:func:`src.core.rca.propagation.propagation_scores`), on the same held-out folds.

Brutal gate (per the directive): the rerank must **improve top-1**, especially on
propagation / dependency faults, and must **not materially regress** self-contained
/ code faults. If it washes like ``tr_calldir`` did (#155), it is killed and the
negative result recorded — same discipline as the shelved abstention gate.

    python scripts/eval_rca_reranker.py --extract --suites re2,re3   # build cache
    python scripts/eval_rca_reranker.py --suites re2,re3             # LOSO report

The cache is a JSONL of per-(case, service) rows (FEATURE_NAMES + label + system +
fault + svc + ``onset``) plus a companion ``*.graph.json`` mapping each case to its
caller->callee edge list — the graph the reranker walks. Every prediction routes
through the **committed** ``RcaRanker`` and ``propagation_scores``, so a green number
is evidence the shipped code reproduces the lift, not just that sklearn can.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from src.core.rca.features import FEATURE_NAMES
from src.core.rca.propagation import propagation_scores
from src.core.rca.ranker import from_dict, serialize_gbc
from src.eval.rcaeval import parse_case_dir_name

BASELINE = {"re3": 0.289, "re2": 0.080}


# ---- extraction (reuses the spike's parquet readers) ---------------------------
def _extract(suites: tuple[str, ...], out: Path) -> None:
    from huggingface_hub import HfApi

    from scripts.spike_multimodal_rca import (
        POST,
        _canon,
        _dl,
        _log_features,
        _metric_features,
        _sec,
        _trace_features,
    )
    from src.core.normalization.fingerprint import fingerprint_message  # noqa: F401  (warms import)
    from src.eval.rcaeval import _infer_level, parse_inject_time

    def _onsets(case: str, inject: float) -> dict[str, float]:
        """Earliest error-log second per service over the incident window."""
        import pyarrow.parquet as pq

        rows = pq.read_table(
            _dl(case, "logs.parquet"), columns=["timestamp", "container_name", "message"]
        ).to_pylist()
        onset: dict[str, float] = {}
        for r in rows:
            s, sec, msg = r["container_name"], _sec(r["timestamp"]), r["message"]
            if not s or sec is None or msg is None or not (inject <= sec <= inject + POST):
                continue
            if _infer_level(str(msg)) == "error" and (s not in onset or sec < onset[s]):
                onset[s] = sec
        return onset

    files = HfApi().list_repo_files("phamquiluan/RCAEval", repo_type="dataset")
    cases = sorted({f.split("/")[0] for f in files if f.startswith(suites) and "/" in f})
    rows: list[dict] = []
    graphs: dict[str, list[list[str]]] = {}
    for case in cases:
        try:
            meta = parse_case_dir_name(case)
            inject = parse_inject_time(open(_dl(case, "inject_time.txt")).read()).timestamp()
            err, grp, stack = _log_features(case, inject)
            rate, dur, graph = _trace_features(case, inject)
            met = _metric_features(case, inject)
            onset = _onsets(case, inject)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {case}: {type(e).__name__} {str(e)[:80]}")
            continue
        graphs[case] = [[a, b] for a, b in sorted(graph.edges)]
        truth = _canon(meta.service)
        has_logs, has_traces, has_metrics = int(bool(err)), int(bool(rate)), int(bool(met))
        for s in set(err) | set(rate) | set(met):
            rows.append({
                "case": case, "system": meta.system, "fault": meta.fault, "svc": meta.service,
                "service": s, "label": int(_canon(s) == truth),
                "log_err": err.get(s, 0), "log_grp": grp.get(s, 0), "log_stack": stack.get(s, 0),
                "tr_rate": rate.get(s, 0.0), "tr_dur": dur.get(s, 0.0), "met_anom": met.get(s, 0.0),
                "has_logs": has_logs, "has_traces": has_traces, "has_metrics": has_metrics,
                "onset": onset.get(s),
            })
        print(f"  {case}: {len(set(err) | set(rate) | set(met))} svcs, {len(graph.edges)} edges")
    out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    Path(str(out) + ".graph.json").write_text(json.dumps(graphs))
    print(f"\nwrote {len(rows)} rows / {len(graphs)} cases -> {out}")


# ---- evaluation ----------------------------------------------------------------
def _train(train_rows: list[dict]):
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier

    X = np.array([[float(r[f]) for f in FEATURE_NAMES] for r in train_rows], dtype=float)
    y = np.array([int(r["label"]) for r in train_rows])
    clf = GradientBoostingClassifier(random_state=0).fit(X, y)
    return from_dict(serialize_gbc(clf, FEATURE_NAMES))


def _graph_for(case: str, graphs: dict) -> "object":
    from src.core.rca.linkage import ServiceGraph

    g = ServiceGraph()
    for a, b in graphs.get(case, []):
        g.edges.add((a, b))
        g.services.update((a, b))
    return g


def _loso(rows: list[dict], graphs: dict, axis: str, **rr) -> dict:
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_case[r["case"]].append(r)
    groups = sorted({r[axis] for r in rows})

    agg = {"base": [0, 0], "rerank": [0, 0]}  # [hits, n]
    per_fault: dict[str, dict[str, list[int]]] = defaultdict(lambda: {"base": [0, 0], "rerank": [0, 0]})
    for held in groups:
        train = [r for r in rows if r[axis] != held]
        if not train or len({r["label"] for r in train}) < 2:
            continue
        ranker = _train(train)
        for case in (c for c, rs in by_case.items() if rs[0][axis] == held):
            cand = by_case[case]
            scored = [(r["service"], ranker.score_vector([float(r[f]) for f in FEATURE_NAMES])) for r in cand]
            truth = {r["service"]: r["label"] for r in cand}
            onset = {r["service"]: r["onset"] for r in cand if r.get("onset") is not None}
            anomaly = {r["service"]: float(r["log_err"]) for r in cand}
            base_top = max(scored, key=lambda t: (t[1], t[0]))[0]
            rr_top = propagation_scores(scored, _graph_for(case, graphs), onset, anomaly, **rr)[0][0]
            fault = cand[0]["fault"]
            for key, top in (("base", base_top), ("rerank", rr_top)):
                agg[key][0] += truth.get(top, 0)
                agg[key][1] += 1
                per_fault[fault][key][0] += truth.get(top, 0)
                per_fault[fault][key][1] += 1
    return {"agg": agg, "per_fault": dict(per_fault)}


def _pct(h: int, n: int) -> str:
    return f"{h}/{n} = {h / n:5.1%}" if n else "  n/a"


def _report(rows: list[dict], graphs: dict, suite: str, **rr) -> None:
    n_cases = len({r["case"] for r in rows})
    print(f"=== propagation reranker LOSO ({n_cases} cases), via committed code ===")
    print(f"logs-volume baseline top-1: {BASELINE.get(suite, float('nan')):.1%}\n")
    for axis, label in (("system", "leave-one-system-out"),
                        ("fault", "leave-one-fault-out"),
                        ("svc", "leave-one-service-out")):
        res = _loso(rows, graphs, axis, **rr)
        b, r = res["agg"]["base"], res["agg"]["rerank"]
        if b[1] == 0:
            print(f"{label}: (no evaluable folds)\n")
            continue
        delta = (r[0] - b[0]) / b[1]
        print(f"{label}")
        print(f"  ranker only : {_pct(*b)}")
        print(f"  + rerank    : {_pct(*r)}   ({delta:+.1%})")
        for fault in sorted(res["per_fault"]):
            fb, fr = res["per_fault"][fault]["base"], res["per_fault"][fault]["rerank"]
            mark = "" if fr[0] >= fb[0] else "  <-- REGRESSION"
            print(f"    {fault:22s} ranker {_pct(*fb)}  ->  rerank {_pct(*fr)}{mark}")
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--suites", default="re3", help="comma-separated RCAEval suites, e.g. re2,re3")
    ap.add_argument("--cache", type=Path, default=Path("/tmp/claude-1000/-home-leonardo-GitHub-raglogs/21cfcd59-4c37-42e9-abfd-55fe8b41ef92/scratchpad/rr_features.jsonl"))
    ap.add_argument("--blend", type=float, default=None)
    ap.add_argument("--direction-weight", type=float, default=None)
    args = ap.parse_args()

    suites = tuple(s.strip() for s in args.suites.split(",") if s.strip())
    if args.extract:
        _extract(suites, args.cache)
    rows = [json.loads(li) for li in args.cache.read_text().splitlines() if li.strip()]
    graphs = json.loads((Path(str(args.cache) + ".graph.json")).read_text())
    rr = {}
    if args.blend is not None:
        rr["blend"] = args.blend
    if args.direction_weight is not None:
        rr["direction_weight"] = args.direction_weight
    # Report per suite so RE2 (resource/network) and RE3 (code) baselines are honest.
    for suite in suites:
        s_rows = [r for r in rows if r["case"].startswith(suite)]
        if s_rows:
            print(f"\n########## suite: {suite} ##########")
            _report(s_rows, graphs, suite, **rr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
