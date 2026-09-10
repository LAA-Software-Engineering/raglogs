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

import subprocess

from src.eval.otel_demo import (
    CHAOS_SCENARIOS,
    FLAG_SCENARIOS,
    generate_chaos_incident,
    generate_incident,
    set_flag_variant,
    set_flag_variant_file,
)
from src.eval.otlp import capture_from_otlp_dir


def _flagd_flip(flagd_url: str):
    def flip(flag: str, variant: str) -> None:
        set_flag_variant(flagd_url, flag, variant)
        print(f"    flagd: {flag} -> {variant}", flush=True)

    return flip


def _flagd_file_flip(flagd_file: Path):
    def flip(flag: str, variant: str) -> None:
        set_flag_variant_file(flagd_file, flag, variant)
        print(f"    flagd(file): {flag} -> {variant}", flush=True)

    return flip


def _dry_flip(flag: str, variant: str) -> None:
    print(f"    [dry] flagd: {flag} -> {variant}", flush=True)


def _dry_capture(ws: datetime, we: datetime):
    return ([{"timestamp": ws.isoformat(), "service": "checkout", "message": "dry-run", "level": "error"}], [], [])


def _kubectl_chaos_hooks(chaos_dir: Path):
    """apply/delete a Chaos Mesh experiment via `kubectl` from a per-scenario
    manifest (deploy/otel-demo/chaos/<scenario.name>.yaml)."""
    def _run(verb: str, sc, extra=()):
        manifest = chaos_dir / f"{sc.name}.yaml"
        subprocess.run(["kubectl", verb, "-f", str(manifest), *extra], check=(verb == "apply"))
        print(f"    kubectl {verb}: {sc.kind}={sc.name}", flush=True)

    return (lambda sc: _run("apply", sc)), (lambda sc: _run("delete", sc, ("--ignore-not-found",)))


def _dry_chaos_hooks():
    def apply(sc):
        print(f"    [dry] kubectl apply: {sc.kind}={sc.name}", flush=True)

    def delete(sc):
        print(f"    [dry] kubectl delete: {sc.kind}={sc.name}", flush=True)

    return apply, delete


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--flagd-url", default="http://localhost:8080")
    ap.add_argument("--flagd-file", type=Path, default=None,
                    help="patch flagd's demo.flagd.json directly (hot-reload) instead of the write API")
    ap.add_argument("--otlp-dir", type=Path, help="Collector OTLP-JSON export dir (required unless --dry-run)")
    ap.add_argument("--out-dir", type=Path, default=Path("data/eval-cases/otel"))
    ap.add_argument("--baseline", type=int, default=300)
    ap.add_argument("--post", type=int, default=600)
    ap.add_argument("--negatives", type=int, default=2, help="how many healthy negative cases")
    ap.add_argument("--chaos", action="store_true",
                    help="generate Chaos-Mesh infra-fault cases (kubectl apply/delete); needs k8s")
    ap.add_argument("--chaos-dir", type=Path, default=Path("deploy/otel-demo/chaos"),
                    help="dir of Chaos-Mesh manifests, one per scenario name")
    ap.add_argument("--dry-run", action="store_true", help="no cluster: validate the loop + case emission")
    args = ap.parse_args()

    if not args.dry_run and args.otlp_dir is None:
        print("--otlp-dir is required unless --dry-run", file=sys.stderr)
        return 2

    capture = _dry_capture if args.dry_run else capture_from_otlp_dir(args.otlp_dir)
    sleep = (lambda _s: None) if args.dry_run else time.sleep
    now = (lambda: datetime.now(timezone.utc))
    written: list[Path] = []

    # Chaos-Mesh infra faults (k8s + Chaos Mesh) — a separate mode from the
    # flag/negative corpus. Reads deploy/otel-demo/chaos/<scenario>.yaml.
    if args.chaos:
        apply_chaos, delete_chaos = _dry_chaos_hooks() if args.dry_run else _kubectl_chaos_hooks(args.chaos_dir)
        for sc in CHAOS_SCENARIOS:
            case_id = f"otel_chaos_{sc.name}"
            print(f"[{len(written)+1}] chaos {sc.kind}={sc.name} ({sc.service})", flush=True)
            out = generate_chaos_incident(
                args.out_dir / case_id, case_id, scenario=sc,
                apply_chaos=apply_chaos, delete_chaos=delete_chaos,
                capture=capture, sleep=sleep, now=now,
                baseline_seconds=args.baseline, post_seconds=args.post,
            )
            written.append(out)
        print(f"\nwrote {len(written)} chaos cases -> {args.out_dir}")
        return 0

    if args.dry_run:
        flip = _dry_flip
    elif args.flagd_file is not None:
        flip = _flagd_file_flip(args.flagd_file)
    else:
        flip = _flagd_flip(args.flagd_url)

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
