#!/usr/bin/env python3
"""Train the RCA confidence calibrator (#118 D / #83) and write its JSON artifact.

The calibrator is Platt scaling on the ranker's ``top_score`` — the model choice
validated in ``docs/eval-rca-calibrator.md`` (a flexible calibrator overfits
out-of-system; Platt roughly halves ECE and generalises).

To avoid calibrating on in-sample ranker scores, the training pairs come from
**out-of-fold** ranker predictions: for each held-out system a ranker trained on
the other systems ranks its cases, and we record ``(top_score, top-1 correct)``.
Platt scaling is then fit on the pooled out-of-fold pairs.

    python scripts/train_rca_calibrator.py --features mm_features.jsonl \
        --out models/rca_calibrator.json

Point the runtime at the result with ``RCA_CALIBRATOR_MODEL_PATH``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

# Reuse the out-of-fold stage-1 + Platt fit from the reliability harness.
_EVAL = Path(__file__).resolve().parent / "eval_rca_calibrator.py"
_spec = importlib.util.spec_from_file_location("eval_rca_calibrator", _EVAL)
_ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ev)


def train(features_path: Path, out_path: Path) -> dict:
    rows = _ev._load(features_path)
    stage1 = _ev._stage1_oof(rows)
    if len({r["correct"] for r in stage1}) < 2:
        raise SystemExit("out-of-fold predictions have a single class; cannot calibrate")
    cal = _ev.fit_platt([r["top_score"] for r in stage1], [r["correct"] for r in stage1])
    artifact = cal.to_dict()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact))
    base = sum(r["correct"] for r in stage1) / len(stage1)
    print(
        f"fit Platt on {len(stage1)} out-of-fold cases (base rate {base:.1%}); "
        f"a={artifact['a']:.3f} b={artifact['b']:.3f}; wrote {out_path}"
    )
    return artifact


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--features", required=True, type=Path, help="feature table JSONL")
    ap.add_argument("--out", required=True, type=Path, help="output artifact JSON path")
    args = ap.parse_args()
    train(args.features, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
