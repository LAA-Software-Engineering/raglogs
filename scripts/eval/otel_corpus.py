#!/usr/bin/env python3
"""Generate the whole OTel-Demo incident corpus in one run (#79).

Loops the committed generator (`src/eval/otel_demo.py`) over every built-in
failure flag, plus healthy negatives and (optionally) a confounded case, writing
one harness case dir per incident under `--out-dir`. Then the frozen external
validation is:

    raglogs frozen-eval data/eval-cases/otel \
        --ranker models/rca_ranker.json --calibrator models/rca_calibrator.json

Prereqs (see docs/eval-otel-demo.md and deploy/otel-demo/): the OTel Demo running
with flagd reachable at --flagd-url and its Collector writing OTLP-JSON to
--otlp-dir (logs.json / traces.json / metrics.json).

Real runs are serial and slow — each case waits `baseline + post` seconds for its
windows to accrue (≈15 min/case at the 300/600 defaults). Use --dry-run first to
validate wiring and case emission without a cluster (no HTTP, no waiting).
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.eval.otel_demo import (
    FLAG_SCENARIOS,
    generate_incident,
    set_flag_variant,
)
from src.eval.otlp import capture_from_otlp_dir


def _real_hooks(flagd_url: str, otlp_dir: Path):
    def flip(flag: str, variant: str) -> None:
        set_flag_variant(flagd_url, flag, variant)
        print(f"    flagd: {flag} -> {variant}", flush=True)

    return flip, capture_from_otlp_dir(otlp_dir), time.sleep


def _dry_hooks():
    """No cluster: log flips, emit one synthetic error log line, don't wait."""
    def flip(flag: str, variant: str) -> None:
        print(f"    [dry] flagd: {flag} -> {variant}", flush=True)

    def capture(ws: datetime, we: datetime):
        return ([{"timestamp": ws.isoformat(), "service": "checkout", "message": "dry-run", "level": "error"}], [], [])

    return flip, capture, (lambda _s: None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--flagd-url", default="http://localhost:8080")
    ap.add_argument("--otlp-dir", type=Path, help="Collector OTLP-JSON export dir (required unless --dry-run)")
    ap.add_argument("--out-dir", type=Path, default=Path("data/eval-cases/otel"))
    ap.add_argument("--baseline", type=int, default=300)
    ap.add_argument("--post", type=int, default=600)
    ap.add_argument("--negatives", type=int, default=2, help="how many healthy negative cases")
    ap.add_argument("--dry-run", action="store_true", help="no cluster: validate the loop + case emission")
    args = ap.parse_args()

    if not args.dry_run and args.otlp_dir is None:
        print("--otlp-dir is required unless --dry-run", file=sys.stderr)
        return 2

    flip, capture, sleep = _dry_hooks() if args.dry_run else _real_hooks(args.flagd_url, args.otlp_dir)
    now = (lambda: datetime.now(timezone.utc))

    written: list[Path] = []
    # one case per built-in failure flag
    for sc in FLAG_SCENARIOS:
        case_id = f"otel_{sc.flag}"
        print(f"[{len(written)+1}] flag {sc.flag} ({sc.service})", flush=True)
        out = generate_incident(
            args.out_dir / case_id, case_id, scenario=sc,
            capture=capture, flip=flip, sleep=sleep, now=now,
            baseline_seconds=args.baseline, post_seconds=args.post,
        )
        written.append(out)
    # healthy negatives (raglogs must abstain)
    for i in range(args.negatives):
        case_id = f"otel_healthy_{i + 1}"
        print(f"[{len(written)+1}] negative {case_id}", flush=True)
        out = generate_incident(
            args.out_dir / case_id, case_id, scenario=None,
            capture=capture, flip=flip, sleep=sleep, now=now,
            baseline_seconds=args.baseline, post_seconds=args.post,
        )
        written.append(out)

    print(f"\nwrote {len(written)} cases -> {args.out_dir}")
    print("next: raglogs frozen-eval", args.out_dir,
          "--ranker models/rca_ranker.json --calibrator models/rca_calibrator.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
