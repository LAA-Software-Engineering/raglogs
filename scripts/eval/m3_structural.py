#!/usr/bin/env python3
"""#209 M3 — run the frozen structural-model evaluation ONCE over a captured corpus.

The protocol (pre-registered on #209 before capture) fixes the model, the metrics and the decision
thresholds; ``src/eval/structural_m3.py`` encodes them. This driver:

1. refuses a dirty working tree (the frozen model must be an exact commit) unless --allow-dirty;
2. records provenance: model commit, corpus content hash, trigger mode, ranker/calibrator paths;
3. ingests every case once (logs + trace/metric sidecars, the harness ingest) and runs the default
   explain path — learned ranker + rare_event trigger — i.e. ``raglogs eval``'s scoring;
4. reads each case's persisted rows over [baseline_start, window_end] exactly as the product's
   structural view does and runs the three arms (M1, M2a, M2a+M2b);
5. writes the JSON report and the as-is markdown post for #209.

    python scripts/eval/m3_structural.py data/eval-cases/otel-m3 \\
        --json m3.json --md m3.md --wipe

Needs Postgres (DB_URL). --wipe truncates the eval tables first (the local eval DB, never a shared one).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

_EVAL_TABLES = ("log_entries, ingestion_jobs, clusters, cluster_runs, cluster_members, cluster_embeddings, "
                "log_embeddings, metric_samples, trace_spans, explanations, ingest_idempotency_keys")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def corpus_hash(cases_dir: Path) -> str:
    """sha256 over every file's relative path and bytes — identifies the exact corpus evaluated."""
    h = hashlib.sha256()
    for path in sorted(p for p in cases_dir.rglob("*") if p.is_file()):
        h.update(str(path.relative_to(cases_dir)).encode() + b"\0")
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", type=Path, help="captured case directory (one sub-dir per case)")
    ap.add_argument("--json", type=Path, required=True, help="write the full report here")
    ap.add_argument("--md", type=Path, required=True, help="write the #209 results post here")
    ap.add_argument("--ranker", default="models/rca_ranker.json")
    ap.add_argument("--calibrator", default="models/rca_calibrator.json")
    ap.add_argument("--wipe", action="store_true", help="truncate the eval tables before ingesting")
    ap.add_argument("--allow-dirty", action="store_true", help="run on an uncommitted tree (not a frozen run)")
    args = ap.parse_args()

    dirty = bool(_git("status", "--porcelain"))
    if dirty and not args.allow_dirty:
        print("refusing: working tree is dirty — the frozen model must be an exact commit "
              "(--allow-dirty for a non-canonical run)", file=sys.stderr)
        return 2

    # The default explain path as pre-registered: learned ranker + rare_event trigger.
    os.environ["TRIGGER_MODE"] = "rare_event"
    os.environ["RCA_RANKER_MODEL_PATH"] = args.ranker
    os.environ["RCA_CALIBRATOR_MODEL_PATH"] = args.calibrator
    from src.config import reload_settings

    settings = reload_settings()

    from sqlalchemy import text

    from src.core.rca.structural import load_structural_rows
    from src.db.session import get_db
    from src.eval.case import load_cases
    from src.eval.report import build_report
    from src.eval.runner import _scope_for, run_cases
    from src.eval.structural_m3 import build_m3_report, evaluate_case, render_markdown
    from src.utils.time import parse_duration

    cases = load_cases(args.cases)
    if not cases:
        print(f"no cases in {args.cases}", file=sys.stderr)
        return 2
    provenance = {
        "model_commit": _git("rev-parse", "HEAD") + (" (DIRTY — not a frozen run)" if dirty else ""),
        "corpus": str(args.cases),
        "corpus_sha256": corpus_hash(args.cases),
        "n_cases": len(cases),
        "trigger_mode": settings.trigger_mode,
        "ranker": args.ranker,
        "calibrator": args.calibrator,
    }

    with get_db() as db:
        if args.wipe:
            db.execute(text(f"TRUNCATE {_EVAL_TABLES} RESTART IDENTITY CASCADE"))
            db.commit()
        results = run_cases(db, cases)  # the one ingest + the default explain path
        db.commit()
        evals = []
        for case in cases:
            baseline = parse_duration(case.baseline_window or settings.default_baseline_window)
            metric_rows, span_rows = load_structural_rows(db, _scope_for(case), case.window_end,
                                                          case.window_start - baseline)
            cause = case.root_cause.service if case.root_cause else None
            evals.append(evaluate_case(case.id, cause, bool(case.expect_explanation and cause), span_rows,
                                       metric_rows, case.window_start))

    report = build_m3_report(evals)
    default = build_report(results)
    report["default_path"] = {k: default[k] for k in ("raglogs", "baseline", "lift_over_baseline")}
    report["provenance"] = provenance
    args.json.write_text(json.dumps(report, indent=1, sort_keys=True, default=str))

    md = render_markdown(report, provenance=provenance)
    r = default["raglogs"]
    md += ("\n**Default path (learned ranker + rare_event):** "
           + ", ".join(f"{k}={r[k]}" for k in sorted(r) if not isinstance(r[k], (dict, list)))
           + f"\n\nLift over baseline: {default['lift_over_baseline']}\n")
    args.md.write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
