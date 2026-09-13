#!/usr/bin/env python3
"""Score methods on the trace-localization benchmark (#118).

Runs the committed pipeline over the synthetic trace-localization corpus
(``scripts/gen_trace_localization_corpus.py``) and reports the causal-localization
metrics from :mod:`src.eval.trace_localization` for three methods:

  baseline   trivial "loudest-error service" (most error-level log lines in the window)
  ranker     the learned ranker's candidate order (RCA_PROPAGATION_RERANK off)
  +rerank    ranker followed by the trace-graph propagation reranker (on)

The headline is **cause_above_symptom**: on the adversarial fault types
(``symptom_only`` / ``latency_only``) the loud symptom outranks the cause, so a method
that scores well is doing causal work. Needs a Postgres/pgvector DB (DB_URL); ingests
each case under its own scope, exactly like ``raglogs frozen-eval``.

    DB_URL=postgresql+psycopg://postgres:postgres@localhost:5433/raglogs \
        python scripts/eval_trace_localization.py data/eval-cases/trace-loc \
        --ranker models/rca_ranker.json --calibrator models/rca_calibrator.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def _loudest_error_service(logs_paths: list[Path], window_start, window_end) -> list[str]:
    """Trivial baseline: services ranked by error-level log volume in the window."""
    from src.utils.time import parse_iso

    err: Counter = Counter()
    for p in logs_paths:
        for line in Path(p).read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (d.get("level") or "").lower() not in ("error", "fatal", "critical"):
                continue
            ts = d.get("timestamp")
            if ts:
                t = parse_iso(str(ts))
                if not (window_start <= t <= window_end):
                    continue
            svc = d.get("service")
            if svc:
                err[svc] += 1
    return [s for s, _ in err.most_common()]


def _ranked_from_result(result) -> list[str]:
    ranked = [c["service"] for c in (result.root_cause_candidates or []) if c.get("service")]
    if not ranked and result.primary_cluster:
        ranked = list(result.primary_cluster.get("services") or [])
    return ranked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--ranker", required=True)
    ap.add_argument("--calibrator", required=True)
    ap.add_argument("--sealed-test", action="store_true",
                    help="ONE-SHOT: score the sealed TEST split instead of DEV. Only for the "
                         "frozen result — never during development.")
    args = ap.parse_args()

    import os
    import uuid

    os.environ["RCA_RANKER_MODEL_PATH"] = args.ranker
    os.environ["RCA_CALIBRATOR_MODEL_PATH"] = args.calibrator
    # Isolate this invocation's scopes so a re-run never sees a prior run's ingested
    # telemetry (same case ids + fixed windows would otherwise pile up in one scope).
    run_id = uuid.uuid4().hex[:8]

    from src.config import reload_settings
    from src.core.explain.summarizer import explain_window
    from src.core.ingestion.service import ingest_files
    from src.db.session import get_db
    from src.eval.case import load_cases
    from src.eval.runner import _ingest_telemetry, _scope_for
    from src.eval.trace_localization import LocResult, load_trace_loc_labels, score_localization

    cases = load_cases(args.corpus)
    labels = {c.id: load_trace_loc_labels(args.corpus / c.id / "case.yaml") for c in cases}
    cases = [c for c in cases if labels.get(c.id)]
    if not cases:
        print(f"no trace-localization cases in {args.corpus}")
        return 1

    # Respect a sealed DEV/TEST split when present: dev loop scores DEV only; TEST is a
    # deliberate one-shot (--sealed-test). No split.yaml -> score everything (back-compat).
    from src.eval.sealed_split import MANIFEST_NAME, dev_ids, load_manifest, test_ids

    if (args.corpus / MANIFEST_NAME).exists():
        manifest = load_manifest(args.corpus)
        keep = set(test_ids(manifest, unseal=True) if args.sealed_test else dev_ids(manifest))
        subset = "SEALED TEST (one-shot)" if args.sealed_test else "DEV"
        cases = [c for c in cases if c.id in keep]
        print(f"[sealed split] scoring {subset}: {len(cases)} cases\n")
    elif args.sealed_test:
        print(f"--sealed-test given but no {MANIFEST_NAME} in {args.corpus}")
        return 1

    methods = {"baseline": [], "ranker": [], "+rerank": []}
    for case in cases:
        tl = labels[case.id]
        base_ranked = _loudest_error_service(case.logs_paths, case.window_start, case.window_end)
        methods["baseline"].append((tl, LocResult(base_ranked)))
        for method, rerank in (("ranker", "false"), ("+rerank", "true")):
            os.environ["RCA_PROPAGATION_RERANK"] = rerank
            reload_settings()
            with get_db() as db:
                scope = _scope_for(case) + f":{run_id}:{method}"
                job, _ = ingest_files(
                    db=db, paths=[str(p) for p in case.logs_paths], recursive=True, scope=scope
                )
                _ingest_telemetry(db, case, scope, job.id)
                result = explain_window(
                    db=db, window_start=case.window_start, window_end=case.window_end,
                    ingestion_job_id=job.id, scope=scope, no_llm=True,
                    baseline_window_str=case.baseline_window,
                )
            methods[method].append((tl, LocResult(_ranked_from_result(result))))

    def _p(v):
        return "  n/a" if v is None else f"{v:6.1%}"

    print(f"=== trace-localization benchmark ({len(cases)} cases) ===\n")
    print(f"{'method':10s}{'top1':>8s}{'top3':>8s}{'cause>sympt':>13s}")
    reports = {m: score_localization(pairs) for m, pairs in methods.items()}
    for m in ("baseline", "ranker", "+rerank"):
        r = reports[m]
        print(f"{m:10s}{_p(r['top1']):>8s}{_p(r['top3']):>8s}{_p(r['cause_above_symptom']):>13s}")
    print("\nper fault type (cause-above-symptom):")
    fts = sorted({ft for r in reports.values() for ft in r["per_fault_type"]})
    print(f"{'fault_type':16s}" + "".join(f"{m:>12s}" for m in ("baseline", "ranker", "+rerank")))
    for ft in fts:
        row = f"{ft:16s}"
        for m in ("baseline", "ranker", "+rerank"):
            cell = reports[m]["per_fault_type"].get(ft, {})
            row += f"{_p(cell.get('cause_above_symptom')):>12s}"
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
