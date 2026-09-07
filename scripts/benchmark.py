#!/usr/bin/env python3
"""Run the raglogs performance benchmark against a live database (issue #85).

Ingests N synthetic log lines and explains the window, recording wall time and
query counts per phase so regressions are visible over time.

    python scripts/benchmark.py                 # default 50k lines
    python scripts/benchmark.py --lines 200000
    python scripts/benchmark.py --json bench_results.json
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="raglogs performance benchmark")
    parser.add_argument("--lines", type=int, default=50_000, help="Synthetic log lines to ingest")
    parser.add_argument("--json", dest="json_out", default=None, help="Write results JSON here")
    args = parser.parse_args()

    from src.db.session import get_db, get_engine
    from src.perf.bench import format_report, run_benchmark

    engine = get_engine()
    with tempfile.TemporaryDirectory() as tmp:
        with get_db() as db:
            result = run_benchmark(db, engine, args.lines, Path(tmp))

    print(format_report(result))

    if args.json_out:
        import json

        Path(args.json_out).write_text(json.dumps(result.to_dict(), indent=2) + "\n")
        print(f"\nWrote {args.json_out}")

    return 0 if result.meets_target else 1


if __name__ == "__main__":
    sys.exit(main())
