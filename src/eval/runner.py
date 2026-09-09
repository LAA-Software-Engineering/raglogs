"""Run both arms over a case directory against a live database.

For each case: ingest its logs under an isolated scope, run raglogs' explain
pipeline (the raglogs arm) and the trivial baseline arm, and normalize each to a
:class:`~src.eval.metrics.Prediction`. This module needs a database; the pure
scoring lives in :mod:`src.eval.metrics` and :mod:`src.eval.baseline`.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from src.core.explain.summarizer import ExplainResult, explain_window
from src.eval.baseline import baseline_prediction
from src.eval.case import EvalCase
from src.eval.metrics import Prediction
from src.utils.time import parse_iso


@dataclass
class CaseResult:
    case: EvalCase
    raglogs: Prediction
    baseline: Prediction


def prediction_from_result(result: ExplainResult) -> Prediction:
    """Normalize an :class:`ExplainResult` into a :class:`Prediction`.

    Pure (no DB), so it is unit-testable with a hand-built result. A ``None``
    primary cluster is the pipeline's "insufficient evidence" signal, so the
    arm is treated as having produced no explanation.
    """
    pc = result.primary_cluster
    produced = pc is not None
    services = list(pc["services"]) if pc and pc.get("services") else []

    top_trigger_ts = None
    for cand in result.trigger_candidates:
        ts = cand.get("timestamp")
        if ts:
            top_trigger_ts = parse_iso(ts) if isinstance(ts, str) else ts
            break

    return Prediction(
        produced_explanation=produced,
        root_cause_service=services[0] if services else None,
        predicted_services=services,
        top_trigger_timestamp=top_trigger_ts,
        returned_any_trigger=bool(result.trigger_candidates),
        confidence=result.confidence,
    )


def _scope_for(case: EvalCase) -> str:
    return f"eval:{case.id}"


def _ingest_telemetry(db: Session, case: EvalCase, scope: str, job_id) -> None:
    """Ingest a case's optional trace/metric sidecars under its scope.

    Additive (#118): logs-only cases have no sidecars and this is a no-op. The
    ingest is idempotent (deterministic PKs + ON CONFLICT), so re-running a case
    does not inflate telemetry counts.
    """
    from src.core.ingestion.telemetry import persist_metric_samples, persist_spans
    from src.eval.rcaeval import load_metrics_jsonl, load_spans_jsonl

    if case.spans_path is not None:
        persist_spans(db, load_spans_jsonl(case.spans_path), scope=scope, ingestion_job_id=job_id)
    if case.metrics_path is not None:
        persist_metric_samples(
            db, load_metrics_jsonl(case.metrics_path), scope=scope, ingestion_job_id=job_id
        )


def run_case(db: Session, case: EvalCase) -> CaseResult:
    """Ingest one case's logs (and any telemetry), then score both arms."""
    from src.core.ingestion.service import ingest_files

    scope = _scope_for(case)
    job, _stats = ingest_files(
        db=db,
        paths=[str(p) for p in case.logs_paths],
        recursive=True,
        scope=scope,
    )
    _ingest_telemetry(db, case, scope, job.id)

    result = explain_window(
        db=db,
        window_start=case.window_start,
        window_end=case.window_end,
        ingestion_job_id=job.id,
        scope=scope,
        no_llm=True,
        baseline_window_str=case.baseline_window,
    )
    raglogs = prediction_from_result(result)
    baseline = baseline_prediction(db, case.window_start, case.window_end, job.id)
    return CaseResult(case=case, raglogs=raglogs, baseline=baseline)


def run_cases(db: Session, cases: list[EvalCase]) -> list[CaseResult]:
    return [run_case(db, case) for case in cases]
