#!/usr/bin/env python3
"""Download external eval corpora and convert them into harness cases.

Currently wires up **RCAEval RE2/RE3** (https://github.com/phamquiluan/RCAEval,
MIT) — 360 labeled failure cases whose ``inject_time.txt`` is exactly the
trigger label the harness needs. Logs plus optional telemetry (traces/metrics,
for #118 multi-modal RCA) are fetched; the raw data and the converted cases are
written to gitignored ``data/`` and are never committed.

Usage:

    python scripts/eval_data.py                 # RE2 + RE3
    python scripts/eval_data.py --suite re2     # one suite
    python scripts/eval_data.py --limit 20      # a quick subset per suite

Then score each suite separately (RE2 = resource/network faults that don't
announce themselves; RE3 = code-level faults visible as stack traces):

    raglogs eval --cases data/eval-cases/rcaeval/re2 --json eval_re2.json
    raglogs eval --cases data/eval-cases/rcaeval/re3 --json eval_re3.json

Other corpora (OTel-demo #79, Loghub-2.0 #80) will plug in here the same way.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAW_DIR = Path("data/rcaeval")
OUT_DIR = Path("data/eval-cases/rcaeval")
REPO_ID = "phamquiluan/RCAEval"


def _download(suite: str) -> Path:
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(
            "huggingface_hub is required to download RCAEval.\n"
            "Install with: pip install 'raglogs[dev]'  (or: pip install huggingface_hub)",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # Fetch the label + logs, plus telemetry (traces/metrics) for multi-modal RCA
    # (#118). Telemetry is optional per case — many RE2 and all sock-shop cases
    # are logs-only — and the converter emits the sidecars only where present.
    print(f"Downloading RCAEval {suite} logs + telemetry into {RAW_DIR}...")
    local = snapshot_download(
        repo_id=REPO_ID,
        repo_type="dataset",
        allow_patterns=[
            f"{suite}*/inject_time.txt",
            f"{suite}*/logs.parquet",
            f"{suite}*/logs.csv",
            f"{suite}*/traces.parquet",
            f"{suite}*/metrics.parquet",
        ],
        local_dir=str(RAW_DIR),
    )
    return Path(local)


def _convert_suite(suite: str, limit: int | None) -> int:
    from src.eval.rcaeval import convert_case

    root = _download(suite)
    # Case directories are named like re2ob_adservice_cpu_1; find any dir that
    # contains the required files, regardless of nesting.
    case_dirs = sorted(
        p.parent
        for p in root.rglob("inject_time.txt")
        if (p.parent / "logs.parquet").exists() or (p.parent / "logs.csv").exists()
    )
    if limit is not None:
        case_dirs = case_dirs[:limit]

    converted = 0
    for src in case_dirs:
        out = OUT_DIR / suite / src.name
        try:
            if convert_case(src, out):
                converted += 1
        except Exception as exc:  # keep going; one bad case shouldn't stop the run
            print(f"  skipped {src.name}: {exc}", file=sys.stderr)
    print(f"{suite}: converted {converted}/{len(case_dirs)} cases into {OUT_DIR / suite}")
    return converted


def main() -> int:
    parser = argparse.ArgumentParser(description="Download + convert eval corpora")
    parser.add_argument("--suite", choices=["re2", "re3", "all"], default="all")
    parser.add_argument("--limit", type=int, default=None, help="Max cases per suite")
    args = parser.parse_args()

    suites = ["re2", "re3"] if args.suite == "all" else [args.suite]
    total = sum(_convert_suite(s, args.limit) for s in suites)
    if total == 0:
        print("No cases converted.", file=sys.stderr)
        return 1
    print(f"\nDone. Run: raglogs eval --cases {OUT_DIR}/re2  (and .../re3)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
