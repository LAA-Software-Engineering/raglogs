"""Aggregate case results into a diffable report: per-arm metrics + lift.

`build_report` is pure (takes results, returns a JSON-able dict) so it is
unit-testable; `render_table` and `write_json` handle presentation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.eval.metrics import (
    ArmScore,
    root_cause_hit,
    score_arm,
    trigger_hit,
)
from src.eval.runner import CaseResult
from src.eval.taxonomy import SCORABLE_AXES, Bucket, build_taxonomy, classify_failure


def _pct(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 4)


def _lift(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return round(a - b, 4)


def _arm_dict(score: ArmScore) -> dict:
    return {
        "root_cause_accuracy": _pct(score.root_cause_accuracy),
        "trigger_accuracy": _pct(score.trigger_accuracy),
        "any_trigger_rate": _pct(score.any_trigger_rate),
        "negative_precision": _pct(score.negative_precision),
        "calibration": {
            conf: {"accuracy": round(acc, 4), "n": n}
            for conf, (acc, n) in score.calibration.items()
        },
    }


def build_report(results: list[CaseResult]) -> dict:
    """Assemble the full report dict (metrics per arm, lift, per-case detail)."""
    raglogs_pairs = [(r.case, r.raglogs) for r in results]
    baseline_pairs = [(r.case, r.baseline) for r in results]
    raglogs_score = score_arm(raglogs_pairs)
    baseline_score = score_arm(baseline_pairs)

    lift = {
        "root_cause_accuracy": _lift(
            raglogs_score.root_cause_accuracy, baseline_score.root_cause_accuracy
        ),
        "trigger_accuracy": _lift(
            raglogs_score.trigger_accuracy, baseline_score.trigger_accuracy
        ),
        "negative_precision": _lift(
            raglogs_score.negative_precision, baseline_score.negative_precision
        ),
    }

    taxonomy = build_taxonomy(raglogs_pairs)

    cases = []
    for r in results:
        bucket = classify_failure(r.case, r.raglogs)
        cases.append(
            {
                "id": r.case.id,
                "expect_explanation": r.case.expect_explanation,
                "expected_service": r.case.root_cause.service if r.case.root_cause else None,
                "failure_bucket": bucket.value if bucket else None,
                "raglogs": {
                    "produced_explanation": r.raglogs.produced_explanation,
                    "root_cause_hit": root_cause_hit(r.case, r.raglogs),
                    "predicted_services": r.raglogs.predicted_services,
                    "trigger_hit": trigger_hit(r.case, r.raglogs),
                    "confidence": r.raglogs.confidence,
                },
                "baseline": {
                    "produced_explanation": r.baseline.produced_explanation,
                    "root_cause_hit": root_cause_hit(r.case, r.baseline),
                    "predicted_services": r.baseline.predicted_services,
                },
            }
        )

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "n_cases": len(results),
        "n_positive": raglogs_score.n_positive,
        "n_negative": raglogs_score.n_negative,
        "raglogs": _arm_dict(raglogs_score),
        "baseline": _arm_dict(baseline_score),
        "lift_over_baseline": lift,
        "failure_taxonomy": {
            "n_scored": taxonomy.n_scored,
            "n_failures": taxonomy.n_failures,
            "failure_rate": _pct(taxonomy.failure_rate),
            "counts": taxonomy.counts,
            "share_of_scored": {b.value: _pct(taxonomy.share_of_scored(b)) for b in Bucket},
            "share_of_failures": {b.value: _pct(taxonomy.share_of_failures(b)) for b in Bucket},
            "case_ids": taxonomy.case_ids,
            "scorable_axes": SCORABLE_AXES,
        },
        "cases": cases,
    }


def _fmt(x: Optional[float]) -> str:
    return "—" if x is None else f"{x * 100:5.1f}%"


def render_table(report: dict) -> str:
    """Render a compact human-readable summary of the report dict."""
    r = report["raglogs"]
    b = report["baseline"]
    lift = report["lift_over_baseline"]
    lines: list[str] = []
    lines.append(
        f"Eval: {report['n_cases']} cases "
        f"({report['n_positive']} positive, {report['n_negative']} negative)"
    )
    lines.append("")
    lines.append(f"{'metric':<24}{'raglogs':>10}{'baseline':>10}{'lift':>10}")
    lines.append("-" * 54)
    for key, label in [
        ("root_cause_accuracy", "root-cause service"),
        ("trigger_accuracy", "trigger"),
        ("negative_precision", "negative precision"),
    ]:
        lines.append(
            f"{label:<24}{_fmt(r.get(key)):>10}{_fmt(b.get(key)):>10}{_fmt(lift.get(key)):>10}"
        )
    lines.append(f"{'any-trigger rate':<24}{_fmt(r.get('any_trigger_rate')):>10}")
    lines.append("")
    lines.append("Confidence calibration (raglogs):")
    if r["calibration"]:
        for conf, cell in r["calibration"].items():
            lines.append(f"  {conf:<10} {_fmt(cell['accuracy'])}  (n={cell['n']})")
    else:
        lines.append("  (no cases)")

    tax = report.get("failure_taxonomy")
    if tax:
        lines.append("")
        lines.append(
            f"Failure taxonomy (raglogs, existing pipeline; n={tax['n_scored']} labeled positive, "
            f"failure rate {_fmt(tax.get('failure_rate'))}):"
        )
        if tax["n_scored"]:
            lines.append(f"  {'bucket':<12}{'of scored':>12}{'of failures':>14}{'n':>6}")
            for bucket in ("correct", "detection", "coverage", "inference"):
                n = tax["counts"].get(bucket, 0)
                of_scored = _fmt(tax["share_of_scored"].get(bucket))
                of_fail = "—" if bucket == "correct" else _fmt(tax["share_of_failures"].get(bucket))
                lines.append(f"  {bucket:<12}{of_scored:>12}{of_fail:>14}{n:>6}")
            lines.append("  (finer structural buckets — observability/ontology/observation-model/")
            lines.append("   non-identifiable — require the Phase H2 shadow eval; see scorable_axes)")
        else:
            lines.append("  (no labeled positive cases)")
    return "\n".join(lines)


def write_json(report: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
