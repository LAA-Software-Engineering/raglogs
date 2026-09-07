#!/usr/bin/env python3
"""Score raglogs normalization against Loghub-2.0 template ground truth (#80).

Loghub-2.0 is **research/academic use only — attribution and citation
required** and must never be vendored or committed. Download it on demand from
Zenodo (record 8275861) into a gitignored directory, then point this at it:

    # 1. Download + unzip Loghub-2.0 from https://zenodo.org/record/8275861
    #    into data/loghub/  (or pass --loghub-dir)
    # 2. Score:
    python scripts/loghub_bench.py --loghub-dir data/loghub --json loghub_results.json

Cite: Jieming Zhu, Shilin He, Pinjia He, Jinyang Liu, Michael R. Lyu.
"Loghub: A Large Collection of System Log Datasets for AI-driven Log
Analytics." ISSRE, 2023.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Systems worth reporting first: different formats and failure modes (#80).
PRIORITY_SYSTEMS = ["BGL", "Thunderbird", "HDFS", "OpenStack"]


def _system_from_filename(path: Path) -> str:
    # e.g. BGL_full.log_structured.csv / Thunderbird_2k.log_structured.csv
    return path.name.split("_")[0]


def find_structured_csvs(loghub_dir: Path) -> dict[str, Path]:
    """Map system name -> its structured CSV, preferring the full dataset."""
    found: dict[str, Path] = {}
    for path in sorted(loghub_dir.rglob("*_structured.csv")):
        system = _system_from_filename(path)
        # Prefer *_full over *_2k when both exist.
        if system not in found or "_full" in path.name:
            found[system] = path
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Loghub-2.0 normalization benchmark")
    parser.add_argument("--loghub-dir", default="data/loghub", help="Downloaded Loghub-2.0 root")
    parser.add_argument("--systems", nargs="*", default=None, help="Systems to score (default: all found)")
    parser.add_argument("--json", dest="json_out", default=None, help="Write results JSON here")
    args = parser.parse_args()

    from src.eval.loghub import score_system

    loghub_dir = Path(args.loghub_dir)
    if not loghub_dir.exists():
        print(
            f"{loghub_dir} not found. Download Loghub-2.0 (research/academic use, "
            "citation required) from https://zenodo.org/record/8275861 and unzip it "
            f"there. See the header of this script for the citation.",
            file=sys.stderr,
        )
        return 1

    available = find_structured_csvs(loghub_dir)
    if not available:
        print(f"No *_structured.csv under {loghub_dir}.", file=sys.stderr)
        return 1

    wanted = args.systems or [s for s in PRIORITY_SYSTEMS if s in available] or list(available)
    results = []
    for system in wanted:
        path = available.get(system)
        if path is None:
            print(f"  {system}: not found, skipping", file=sys.stderr)
            continue
        from src.eval.loghub import load_structured_csv

        rows = load_structured_csv(path.read_text())
        res = score_system(system, rows)
        results.append(res)
        print(
            f"{res.system:<14} GA={res.grouping_accuracy:6.1%}  "
            f"templates {res.induced_templates:>5} / {res.ground_truth_templates:<5} "
            f"(ratio {res.template_count_ratio:.2f})  [{res.n_lines:,} lines]"
        )

    if not results:
        return 1
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([r.to_dict() for r in results], indent=2) + "\n"
        )
        print(f"\nWrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
