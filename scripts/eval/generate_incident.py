#!/usr/bin/env python3
"""Generate one OTel-Demo incident case (#79): baseline → flip flag → incident →
emit case → flip back. See ``docs/eval-otel-demo.md`` for the full procedure and
the frozen external-validation protocol.

    # positive case (flip a built-in failure flag)
    python scripts/eval/generate_incident.py --flag paymentServiceFailure \
        --flagd-url http://localhost:8080 --otlp-dir ./capture \
        --out data/eval-cases/otel/payment_1

    # healthy negative case (no flag flipped; raglogs must abstain)
    python scripts/eval/generate_incident.py --negative \
        --otlp-dir ./capture --out data/eval-cases/otel/healthy_1

``--otlp-dir`` holds ``logs.json`` / ``traces.json`` / ``metrics.json`` (OTLP-JSON
from the demo's bundled OTel Collector file exporter — no SaaS backend needed).
The tool records the exact flag-flip time as the ground-truth trigger.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.eval.otel_demo import (
    SCENARIOS_BY_FLAG,
    generate_incident,
    set_flag_variant,
)
from src.eval.otlp import capture_from_otlp_dir


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--flag", help="flagd failure flag (omit with --negative)")
    ap.add_argument("--negative", action="store_true", help="healthy case: flip nothing")
    ap.add_argument("--flagd-url", default="http://localhost:8080")
    ap.add_argument("--otlp-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--baseline", type=int, default=300)
    ap.add_argument("--post", type=int, default=600)
    ap.add_argument("--case-id", default=None)
    args = ap.parse_args()

    if args.negative:
        scenario = None
    elif args.flag in SCENARIOS_BY_FLAG:
        scenario = SCENARIOS_BY_FLAG[args.flag]
    else:
        print(f"unknown --flag {args.flag!r}; known: {', '.join(SCENARIOS_BY_FLAG)}", file=sys.stderr)
        return 2

    case_id = args.case_id or args.out.name

    def flip(flag: str, variant: str) -> None:
        set_flag_variant(args.flagd_url, flag, variant)
        print(f"  flagd: {flag} -> {variant}", flush=True)

    out = generate_incident(
        args.out,
        case_id,
        scenario=scenario,
        capture=capture_from_otlp_dir(args.otlp_dir),
        flip=flip,
        sleep=time.sleep,
        now=lambda: datetime.now(timezone.utc),
        baseline_seconds=args.baseline,
        post_seconds=args.post,
    )
    print(f"wrote case -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
