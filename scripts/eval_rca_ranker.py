#!/usr/bin/env python3
"""Leave-one-*-out evaluation of the learned RCA ranker (#118 C2b).

Unlike the exploratory spike (``spike_multimodal_rca.py``, which scored sklearn
directly), this routes every prediction through the **committed** ranker code:
each fold trains a ``GradientBoostingClassifier``, serialises it with
:func:`src.core.rca.ranker.serialize_gbc`, and scores held-out candidates with
:meth:`src.core.rca.ranker.RcaRanker.score_vector` — the pure-Python evaluator
the runtime will use. So a green number here is evidence the *shipped* artifact
path reproduces the lift, not just that sklearn can.

Leakage discipline (per #118): three held-out axes, never a random split —

    leave-one-system-out    train on 2 systems, test on the 3rd (generalises OOD)
    leave-one-fault-out     train on other fault classes, test on the held-out one
    leave-one-service-out   train on other injected services, test on the held-out

Top-1: for each held-out case, rank its candidate services and count a hit when
the top-scored service is the injected root cause.

    python scripts/eval_rca_ranker.py --features mm_features.jsonl

The feature table is one JSON row per ``(case, service)`` with FEATURE_NAMES +
``label`` + ``system`` + ``case`` (built by ``spike_multimodal_rca.py --extract``
or, authoritatively, pipeline extraction via ``compute_features``).
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from src.core.rca.features import FEATURE_NAMES
from src.core.rca.ranker import from_dict, serialize_gbc
from src.eval.rcaeval import parse_case_dir_name

# Trivial logs-volume baseline top-1 (see docs/rca-benchmark.md).
BASELINE = {"re3": 0.289, "re2": 0.080}


def _load(path: Path) -> list[dict]:
    rows = [json.loads(li) for li in path.read_text().splitlines() if li.strip()]
    for r in rows:
        # Derive the held-out axes from the case id when not already present.
        if "fault" not in r or "svc" not in r:
            meta = parse_case_dir_name(r["case"])
            r["fault"], r["svc"] = meta.fault, meta.service
    return rows


def _train_ranker(train_rows: list[dict]):
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier

    X = np.array([[float(r[f]) for f in FEATURE_NAMES] for r in train_rows], dtype=float)
    y = np.array([int(r["label"]) for r in train_rows])
    clf = GradientBoostingClassifier(random_state=0).fit(X, y)
    return from_dict(serialize_gbc(clf, FEATURE_NAMES))


def _leave_one_out(rows: list[dict], axis: str) -> tuple[dict, int, int]:
    """Hold out each value of ``axis`` (system|fault|svc) in turn."""
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_case[r["case"]].append(r)
    groups = sorted({r[axis] for r in rows})

    per_group, hits, total = {}, 0, 0
    for held in groups:
        train = [r for r in rows if r[axis] != held]
        if not train or len({r["label"] for r in train}) < 2:
            continue
        ranker = _train_ranker(train)
        cases = [c for c, rs in by_case.items() if rs[0][axis] == held]
        h = 0
        for c in cases:
            cand = by_case[c]
            top = max(cand, key=lambda r: ranker.score_vector([float(r[f]) for f in FEATURE_NAMES]))
            h += int(top["label"] == 1)
        per_group[held] = (h, len(cases))
        hits += h
        total += len(cases)
    return per_group, hits, total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--features", required=True, type=Path)
    ap.add_argument("--suite", default="re3", choices=["re2", "re3"], help="baseline to compare against")
    args = ap.parse_args()

    rows = _load(args.features)
    n_cases = len({r["case"] for r in rows})
    oracle = sum(
        any(r["label"] for r in rs)
        for rs in _by_case(rows).values()
    )
    baseline = BASELINE[args.suite]
    print(f"=== RCA ranker leave-one-*-out ({args.suite}, {n_cases} cases), via committed RcaRanker ===")
    print(f"oracle ceiling (truth is a candidate): {oracle}/{n_cases} = {oracle / n_cases:.1%}")
    print(f"logs-volume baseline top-1: {baseline:.1%}\n")

    for axis, label in (("system", "leave-one-system-out"),
                        ("fault", "leave-one-fault-out"),
                        ("svc", "leave-one-service-out")):
        per, h, n = _leave_one_out(rows, axis)
        if n == 0:
            print(f"{label:24s} (no evaluable folds)")
            continue
        delta = h / n - baseline
        detail = "  ".join(f"{g} {per[g][0]}/{per[g][1]}" for g in sorted(per))
        print(f"{label:24s} {h}/{n} = {h / n:.1%}  ({delta:+.1%} vs baseline)")
        print(f"{'':24s} {detail}\n")
    return 0


def _by_case(rows: list[dict]) -> dict[str, list[dict]]:
    d: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        d[r["case"]].append(r)
    return d


if __name__ == "__main__":
    raise SystemExit(main())
