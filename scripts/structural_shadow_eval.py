#!/usr/bin/env python3
"""Run the Phase H2 structural shadow eval over an eval-case corpus and print a report.

Offline / shadow: reads each case's ``metrics.jsonl`` + ``spans.jsonl`` directly (no DB, no ingestion),
runs the A–E structural core, and scores the five outcomes against the labeled root cause. Changes
nothing in the product explain path — see ``src/eval/structural_shadow.py`` and
``docs/eval-structural-shadow.md``.

Usage:
    python scripts/structural_shadow_eval.py data/eval-cases/trace-loc
"""
from __future__ import annotations

import sys
from collections import defaultdict

from src.eval.structural_shadow import load_and_run


def main(cases_dir: str) -> None:
    results, score = load_and_run(cases_dir)
    print(f"Structural shadow eval — {cases_dir}  (candidate recall, NOT structural correctness)")
    print(f"  n={score.n}  candidate_recall={score.candidate_recall:.1%}  "
          f"unique={score.unique_rate:.1%}  abstain={score.abstention_rate:.1%}")
    print(f"  selectivity: mean_candidates={score.mean_candidates:.1f}  "
          f"mean_candidate_ratio={score.mean_candidate_ratio:.1%} of services (1.0 = enumerate all)")
    print(f"  outcomes: {score.outcome_counts}")

    # Per fault-type breakdown when the case ids encode it (…_<fault_type>_<n>), e.g. trace-loc.
    by_ft: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in results:
        parts = r.case_id.split("_")
        ft = "_".join(parts[2:-1]) if len(parts) >= 4 and parts[-1].isdigit() else ""
        if ft:
            by_ft[ft][0] += 1
            by_ft[ft][1] += int(r.truth_retained)
            by_ft[ft][2] += int(r.unique)
    if by_ft:
        print("  by fault-type (truth_retained / unique):")
        for ft, (n, ok, u) in sorted(by_ft.items()):
            print(f"    {ft:14} n={n}  truth_retained={ok}/{n}  unique={u}/{n}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    main(sys.argv[1])
