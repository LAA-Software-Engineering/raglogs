#!/usr/bin/env python3
"""Reliability of the RCA confidence calibrator (#118 D / #83), nested leave-one-
system-out — no leakage for either model.

Two stages, both routed through committed code:

  1. Out-of-fold ranker predictions. For each held-out system, a ranker trained
     on the *other* systems ranks its cases (via ``RcaRanker.score_vector``); per
     case we record the calibration features (``calibration_features``) and the
     label ``top-1 correct``. This yields one (features, correct) row per case.

  2. Out-of-fold calibrator. For each held-out system, a calibrator trained on the
     other systems' stage-1 rows predicts P(top-1 correct) on the held-out rows.
     We compare that against the naive baseline of using the ranker's raw
     ``top_score`` as the confidence.

Reported: expected calibration error (ECE, 10 bins) for raw ``top_score`` vs the
calibrated probability, plus per-bin reliability. A lower ECE means the number is
a more honest probability — the #83 fix.

    python scripts/eval_rca_calibrator.py --features mm_features.jsonl --suite re3
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from src.core.rca.calibration import PlattCalibrator, calibration_features
from src.core.rca.candidates import build_candidates
from src.core.rca.features import FEATURE_NAMES, FeatureTable, ServiceFeatures
from src.core.rca.ranker import from_dict, serialize_gbc


def _load(path: Path) -> list[dict]:
    return [json.loads(li) for li in path.read_text().splitlines() if li.strip()]


def _fit_ranker(rows: list[dict]):
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier

    clf = GradientBoostingClassifier(random_state=0).fit(
        np.array([[r[f] for f in FEATURE_NAMES] for r in rows], dtype=float),
        np.array([r["label"] for r in rows]),
    )
    return from_dict(serialize_gbc(clf, FEATURE_NAMES))


def fit_platt(top_scores: list[float], correct: list[int]) -> PlattCalibrator:
    """Platt scaling: 1-D logistic of P(top-1 correct) on the ranker top_score."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    lr = LogisticRegression(max_iter=1000).fit(
        np.array(top_scores, dtype=float).reshape(-1, 1), np.array(correct)
    )
    return PlattCalibrator(a=float(lr.coef_[0][0]), b=float(lr.intercept_[0]), feature="top_score")


def _rows_to_table(rows: list[dict]) -> FeatureTable:
    return FeatureTable(services=[
        ServiceFeatures(service=r["service"], **{f: float(r[f]) for f in FEATURE_NAMES})
        for r in rows
    ])


def _stage1_oof(rows: list[dict]) -> list[dict]:
    """One (calibration features, correct, system) row per case, from an out-of-
    fold ranker (trained on the other systems)."""
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_case[r["case"]].append(r)
    systems = sorted({r["system"] for r in rows})

    out: list[dict] = []
    for held in systems:
        train = [r for r in rows if r["system"] != held]
        if len({r["label"] for r in train}) < 2:
            continue
        ranker = _fit_ranker(train)
        for case, crows in by_case.items():
            if crows[0]["system"] != held:
                continue
            cands = build_candidates(_rows_to_table(crows), scorer=ranker.score)
            if not cands:
                continue
            label_by_svc = {r["service"]: int(r["label"]) for r in crows}
            correct = int(label_by_svc.get(cands[0].service, 0) == 1)
            feats = calibration_features(cands)
            out.append({"system": held, "correct": correct, **feats})
    return out


def _ece(pairs: list[tuple[float, int]], n_bins: int = 10) -> float:
    """Expected calibration error over ``(prob, correct)`` pairs."""
    if not pairs:
        return 0.0
    total = len(pairs)
    ece = 0.0
    for b in range(n_bins):
        lo, hi = b / n_bins, (b + 1) / n_bins
        bucket = [(p, y) for p, y in pairs if (lo < p <= hi) or (b == 0 and p <= 0)]
        if not bucket:
            continue
        conf = sum(p for p, _ in bucket) / len(bucket)
        acc = sum(y for _, y in bucket) / len(bucket)
        ece += abs(acc - conf) * (len(bucket) / total)
    return ece


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--features", required=True, type=Path)
    ap.add_argument("--suite", default="re3")
    args = ap.parse_args()

    rows = _load(args.features)
    stage1 = _stage1_oof(rows)
    systems = sorted({r["system"] for r in stage1})
    base_rate = sum(r["correct"] for r in stage1) / max(len(stage1), 1)

    raw_pairs: list[tuple[float, int]] = []
    cal_pairs: list[tuple[float, int]] = []
    for held in systems:
        train = [r for r in stage1 if r["system"] != held]
        test = [r for r in stage1 if r["system"] == held]
        if len({r["correct"] for r in train}) < 2:
            continue
        cal = fit_platt([r["top_score"] for r in train], [r["correct"] for r in train])
        for r in test:
            raw_pairs.append((float(r["top_score"]), r["correct"]))
            cal_pairs.append((cal.probability(r["top_score"]), r["correct"]))

    print(f"=== RCA calibrator reliability ({args.suite}, {len(stage1)} cases), nested LOSO ===")
    print(f"top-1 accuracy (base rate): {base_rate:.1%}")
    print(f"ECE raw ranker top_score : {_ece(raw_pairs):.3f}")
    print(f"ECE Platt P(top-1)       : {_ece(cal_pairs):.3f}")
    print("\nreliability (calibrated): bin  n   mean_conf  acc")
    n_bins = 10
    for b in range(n_bins):
        lo, hi = b / n_bins, (b + 1) / n_bins
        bucket = [(p, y) for p, y in cal_pairs if (lo < p <= hi) or (b == 0 and p <= 0)]
        if not bucket:
            continue
        conf = sum(p for p, _ in bucket) / len(bucket)
        acc = sum(y for _, y in bucket) / len(bucket)
        print(f"  {lo:.1f}-{hi:.1f}  {len(bucket):3d}   {conf:.2f}      {acc:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
