"""Frozen external-validation scorer (#79 / #118).

Scores an independently-generated corpus (the OTel Demo, #79) with a ranker +
calibrator **trained only on RCAEval** and never touched afterwards. This is the
external-validity test: a 60% RE3 / 77% RE2 leave-one-system-out result can be
real and still degrade on a genuinely different deployment, so the number that
matters is how the *frozen* artifact behaves on cases it has never seen.

The scoring is pure (this module) — mapping an ``ExplainResult`` + the case's
ground truth to per-case facts, then aggregating — so it is unit-testable on
synthetic fixtures. Those fixtures test **plumbing and metric math only**; the
scorer, model, and calibrator are frozen before any real OTel result is seen, or
the third corpus quietly becomes training data.

Metrics: root-cause top-1/top-3, abstention on healthy negatives, calibrated-
confidence reliability (ECE), deploy-trigger correctness, confounded-case
correctness, and per-fault-class / per-modality breakdowns.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from src.core.explain.summarizer import ExplainResult
from src.eval.case import EvalCase
from src.utils.time import parse_iso

TRIGGER_TOLERANCE = timedelta(minutes=5)


@dataclass
class FrozenCaseResult:
    case_id: str
    fault_class: str  # trigger type, e.g. deploy | dependency | resource | code | none
    is_negative: bool
    is_confounded: bool
    truth_service: Optional[str]
    ranked_services: list[str]
    confidence: Optional[float]  # calibrated P(top-1 correct)
    produced: bool
    trigger_correct: Optional[bool]
    modalities: str  # e.g. "logs+metrics"
    # Abstention gate decision (#79), computed independently of the ranker so the
    # ranker is still scored on every incident window (a good gate must not be able
    # to hide bad ranker cases by abstaining on them). None = gate not evaluated.
    abstained: Optional[bool] = None

    @property
    def correct_top1(self) -> bool:
        return bool(self.truth_service) and self.truth_service in self.ranked_services[:1]

    @property
    def correct_top3(self) -> bool:
        return bool(self.truth_service) and self.truth_service in self.ranked_services[:3]


def _modalities(case: EvalCase) -> str:
    mods = ["logs"]
    if case.spans_path is not None:
        mods.append("traces")
    if case.metrics_path is not None:
        mods.append("metrics")
    return "+".join(mods)


def frozen_case_result(
    case: EvalCase, result: ExplainResult, abstained: Optional[bool] = None
) -> FrozenCaseResult:
    """Map one case + its ExplainResult into scoring facts (pure). ``abstained`` is
    the abstention gate's decision, computed separately from ``result`` (which is
    the ranker run with the gate OFF, so the ranker is scored on every incident)."""
    ranked = [c["service"] for c in (result.root_cause_candidates or []) if c.get("service")]
    if not ranked and result.primary_cluster:
        ranked = list(result.primary_cluster.get("services") or [])
    produced = result.predicted_root_cause is not None or result.primary_cluster is not None

    trigger_correct: Optional[bool] = None
    if case.trigger is not None and case.trigger.timestamp is not None:
        top_ts = None
        for cand in result.trigger_candidates:
            ts = cand.get("timestamp")
            if ts:
                top_ts = parse_iso(ts) if isinstance(ts, str) else ts
                break
        trigger_correct = top_ts is not None and abs(top_ts - case.trigger.timestamp) <= TRIGGER_TOLERANCE

    return FrozenCaseResult(
        case_id=case.id,
        fault_class=(case.trigger.type if case.trigger else "none"),
        is_negative=not case.expect_explanation,
        is_confounded="confounder" in (case.notes or "").lower(),
        truth_service=(case.root_cause.service if case.root_cause else None),
        ranked_services=ranked,
        confidence=result.predicted_root_cause_confidence,
        produced=produced,
        trigger_correct=trigger_correct,
        modalities=_modalities(case),
        abstained=abstained,
    )


def ece(pairs: list[tuple[float, int]], n_bins: int = 10) -> float:
    """Expected calibration error over ``(prob, correct)`` pairs."""
    if not pairs:
        return 0.0
    total = len(pairs)
    out = 0.0
    for b in range(n_bins):
        lo, hi = b / n_bins, (b + 1) / n_bins
        bucket = [(p, y) for p, y in pairs if (lo < p <= hi) or (b == 0 and p <= 0)]
        if not bucket:
            continue
        conf = sum(p for p, _ in bucket) / len(bucket)
        acc = sum(y for _, y in bucket) / len(bucket)
        out += abs(acc - conf) * (len(bucket) / total)
    return out


def _rate(flags: list[bool]) -> Optional[float]:
    return (sum(1 for f in flags if f) / len(flags)) if flags else None


def score_frozen(results: list[FrozenCaseResult]) -> dict:
    """Aggregate the frozen-validation metrics."""
    positives = [r for r in results if not r.is_negative and r.truth_service]
    negatives = [r for r in results if r.is_negative]
    confounded = [r for r in results if r.is_confounded and r.truth_service]

    conf_pairs = [(r.confidence, int(r.correct_top1)) for r in positives if r.confidence is not None]

    by_fault: dict[str, dict] = {}
    for cls in sorted({r.fault_class for r in positives}):
        grp = [r for r in positives if r.fault_class == cls]
        by_fault[cls] = {"n": len(grp), "top1": _rate([r.correct_top1 for r in grp])}

    by_modality: dict[str, dict] = {}
    for mod in sorted({r.modalities for r in positives}):
        grp = [r for r in positives if r.modalities == mod]
        by_modality[mod] = {"n": len(grp), "top1": _rate([r.correct_top1 for r in grp])}

    deploy = [r for r in positives if r.fault_class == "deploy" and r.trigger_correct is not None]

    # Whole-system views (#79): the ranker metrics above are over ALL positives,
    # independent of the gate (a good gate must not hide bad ranker cases). The gate
    # is scored separately, then composed end-to-end over the cases it lets through.
    gated_pos = [r for r in positives if r.abstained is not None]
    gated_neg = [r for r in negatives if r.abstained is not None]
    covered_pos = [r for r in gated_pos if not r.abstained]  # incidents we proceed on
    gate = {
        # component: gate alone
        "incident_recall": _rate([not r.abstained for r in gated_pos]),
        "healthy_abstention": _rate([bool(r.abstained) for r in gated_neg]),
        # end-to-end: ranker gated by abstention. overall_coverage spans incidents
        # AND healthy negatives (incident-only coverage == incident_recall above).
        "overall_coverage": _rate([not r.abstained for r in gated_pos + gated_neg]),
        "selective_top1": _rate([r.correct_top1 for r in covered_pos]),
        "false_diagnosis_rate": _rate([not r.abstained for r in gated_neg]),
    } if (gated_pos or gated_neg) else None

    return {
        "n_cases": len(results),
        "n_positive": len(positives),
        "n_negative": len(negatives),
        "top1": _rate([r.correct_top1 for r in positives]),
        "top3": _rate([r.correct_top3 for r in positives]),
        "negative_abstention": _rate([not r.produced for r in negatives]),
        "confidence_ece": ece(conf_pairs) if conf_pairs else None,
        "deploy_trigger_correct": _rate([bool(r.trigger_correct) for r in deploy]),
        "confounded_trigger_correct": _rate(
            [bool(r.trigger_correct) for r in confounded if r.trigger_correct is not None]
        ),
        "gate": gate,
        "per_fault_class": by_fault,
        "per_modality": by_modality,
        # Per-case detail so a result is diagnosable without re-running (which
        # service was predicted vs the truth, top-3, confidence).
        "cases": [
            {
                "id": r.case_id,
                "fault_class": r.fault_class,
                "is_negative": r.is_negative,
                "truth_service": r.truth_service,
                "predicted_services": r.ranked_services[:3],
                "correct_top1": r.correct_top1,
                "correct_top3": r.correct_top3,
                "confidence": r.confidence,
                "produced": r.produced,
                "abstained": r.abstained,
            }
            for r in results
        ],
    }


PROVENANCE_MODE = "frozen external validation"


def provenance_header(*, ranker_path: str, calibrator_path: str) -> list[str]:
    """The explicit, publishable frozen-validation banner — makes it hard to later
    misrepresent the result as anything but zero-OTel-training external validation."""
    return [
        "Model trained on:            RCAEval RE2 + RE3",
        "Calibrator trained on:       RCAEval out-of-fold predictions",
        "OTel cases seen in training: 0",
        "Model changes after capture: 0",
        f"Evaluation mode:             {PROVENANCE_MODE}",
        f"Ranker artifact:             {ranker_path}",
        f"Calibrator artifact:         {calibrator_path}",
    ]


def _pct(v: Optional[float]) -> str:
    return "  n/a" if v is None else f"{v:6.1%}"


def render_frozen_report(
    report: dict, *, ranker_path: str, calibrator_path: str, gate_params: Optional[dict] = None
) -> str:
    ece_val = report["confidence_ece"]
    ece_str = "  n/a" if ece_val is None else f"{ece_val:6.3f}"
    lines = ["=== RCA frozen external validation (#79) ===", ""]
    lines += provenance_header(ranker_path=ranker_path, calibrator_path=calibrator_path)
    if gate_params:
        lines += [
            "Abstention gate:",
            f"  modalities:              {gate_params['modalities']}",
            f"  tau_log:                 {gate_params['tau_log']}",
            f"  tau_metric:              {gate_params['tau_metric']}",
            f"  threshold:               {gate_params['threshold']}",
            f"  enabled in ranker pass:  {str(gate_params['enabled_in_ranker_pass']).lower()}",
        ]
    lines += [
        "",
        f"cases: {report['n_cases']}  (positive {report['n_positive']}, negative {report['n_negative']})",
        "",
        "component — ranker (ALL incident windows, gate off):",
        f"  {'root-cause top-1':<26}{_pct(report['top1'])}",
        f"  {'root-cause top-3':<26}{_pct(report['top3'])}",
        f"  {'confidence ECE':<26}{ece_str}",
        f"  {'deploy-trigger correct':<26}{_pct(report['deploy_trigger_correct'])}",
        f"  {'confounded-trigger correct':<26}{_pct(report['confounded_trigger_correct'])}",
    ]
    gate = report.get("gate")
    if gate:
        lines += [
            "",
            "component — abstention gate:",
            f"  {'incident recall':<26}{_pct(gate['incident_recall'])}",
            f"  {'healthy abstention':<26}{_pct(gate['healthy_abstention'])}",
            "",
            "end-to-end (ranker gated by abstention):",
            f"  {'overall coverage':<26}{_pct(gate['overall_coverage'])}",
            f"  {'selective top-1':<26}{_pct(gate['selective_top1'])}",
            f"  {'false-diagnosis (healthy)':<26}{_pct(gate['false_diagnosis_rate'])}",
        ]
    else:
        lines.append(f"{'abstention on negatives':<28}{_pct(report['negative_abstention'])}")
    lines += [
        "",
        "per fault class:",
    ]
    for cls, m in report["per_fault_class"].items():
        lines.append(f"  {cls:<24}{_pct(m['top1'])}  (n={m['n']})")
    lines.append("per modality:")
    for mod, m in report["per_modality"].items():
        lines.append(f"  {mod:<24}{_pct(m['top1'])}  (n={m['n']})")
    return "\n".join(lines)
