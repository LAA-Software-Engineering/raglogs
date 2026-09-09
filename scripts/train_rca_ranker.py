#!/usr/bin/env python3
"""Train the multi-modal RCA ranker (#118 C2) and write a non-pickle JSON artifact.

Reads a feature table JSONL — one row per ``(case, candidate service)`` with the
:data:`~src.core.rca.features.FEATURE_NAMES` columns plus a ``label`` (1 if the
service is the injected root cause) — fits a gradient-boosted tree ensemble, and
serialises it via :func:`src.core.rca.ranker.serialize_gbc` into a plain-JSON
artifact the runtime loads without sklearn (see ``ranker.py``).

    python scripts/train_rca_ranker.py --features mm_features.jsonl \
        --out models/rca_ranker.json

The feature table is produced offline (the multi-modal spike's ``--extract``, or
pipeline extraction via ``compute_features``). Training uses sklearn; the artifact
does not. Point the runtime at the result with ``RCA_RANKER_MODEL_PATH``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.core.rca.features import FEATURE_NAMES
from src.core.rca.ranker import from_dict, serialize_gbc


def _load_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def train(features_path: Path, out_path: Path, random_state: int = 0) -> dict:
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier

    rows = _load_rows(features_path)
    if not rows:
        raise SystemExit(f"no feature rows in {features_path}")
    X = np.array([[float(r.get(f, 0)) for f in FEATURE_NAMES] for r in rows], dtype=float)
    y = np.array([int(r["label"]) for r in rows])
    if len(set(y.tolist())) < 2:
        raise SystemExit("feature table has a single class; cannot train a ranker")

    clf = GradientBoostingClassifier(random_state=random_state).fit(X, y)
    artifact = serialize_gbc(clf, FEATURE_NAMES)

    # Self-check: the pure-Python evaluator must reproduce sklearn's probability.
    ranker = from_dict(artifact)
    proba = clf.predict_proba(X)[:, 1]
    max_err = max(abs(ranker.score_vector(X[i].tolist()) - proba[i]) for i in range(len(rows)))
    if max_err > 1e-6:
        raise SystemExit(f"serialised ranker diverges from sklearn (max err {max_err:.2e})")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact))
    print(f"trained on {len(rows)} rows ({int(y.sum())} positive); "
          f"parity max_err={max_err:.2e}; wrote {out_path}")
    return artifact


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--features", required=True, type=Path, help="feature table JSONL")
    ap.add_argument("--out", required=True, type=Path, help="output artifact JSON path")
    ap.add_argument("--random-state", type=int, default=0)
    args = ap.parse_args()
    train(args.features, args.out, args.random_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
