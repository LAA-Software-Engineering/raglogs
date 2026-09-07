"""Unit tests for the eval harness pure logic (no database)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.explain.summarizer import ExplainResult
from src.eval.baseline import baseline_from_counts
from src.eval.case import EvalCase, RootCause, Trigger, load_cases
from src.eval.metrics import (
    Prediction,
    negative_correct,
    root_cause_hit,
    score_arm,
    trigger_hit,
)
from src.eval.report import build_report
from src.eval.runner import CaseResult, prediction_from_result

CASES_DIR = Path(__file__).resolve().parents[1] / "eval" / "cases"
T0 = datetime(2026, 3, 16, 15, 0, 0, tzinfo=timezone.utc)


def _pos_case(service="billing-worker", trigger_ts=None) -> EvalCase:
    return EvalCase(
        id="pos",
        window_start=T0,
        window_end=T0 + timedelta(hours=1),
        expect_explanation=True,
        root_cause=RootCause(service=service),
        trigger=Trigger(timestamp=trigger_ts, type="deploy") if trigger_ts else None,
    )


def _neg_case() -> EvalCase:
    return EvalCase(
        id="neg",
        window_start=T0,
        window_end=T0 + timedelta(hours=1),
        expect_explanation=False,
    )


# ── Case loading ──────────────────────────────────────────────────────────────


class TestLoadCases:
    def test_loads_seed_cases(self):
        cases = load_cases(CASES_DIR)
        by_id = {c.id: c for c in cases}
        assert "sample-incident-001" in by_id
        assert "quiet-healthy-002" in by_id
        assert "steady-state-003" in by_id

    def test_positive_case_fields(self):
        case = next(c for c in load_cases(CASES_DIR) if c.id == "sample-incident-001")
        assert case.expect_explanation is True
        assert case.root_cause.service == "billing-worker"
        assert case.trigger.type == "deploy"
        assert case.trigger.timestamp is not None
        assert case.logs_paths  # resolved to real files

    def test_negative_case_has_no_root_cause(self):
        case = next(c for c in load_cases(CASES_DIR) if c.id == "quiet-healthy-002")
        assert case.expect_explanation is False
        assert case.root_cause is None


# ── Metrics ───────────────────────────────────────────────────────────────────


class TestMetrics:
    def test_root_cause_hit(self):
        case = _pos_case(service="billing-worker")
        assert root_cause_hit(case, Prediction(True, predicted_services=["billing-worker"])) is True
        assert root_cause_hit(case, Prediction(True, predicted_services=["api"])) is False
        # No label on negative cases.
        assert root_cause_hit(_neg_case(), Prediction(True)) is None

    def test_trigger_hit_within_and_outside_tolerance(self):
        label = T0 + timedelta(minutes=15)
        case = _pos_case(trigger_ts=label)
        near = Prediction(True, top_trigger_timestamp=label + timedelta(minutes=3), returned_any_trigger=True)
        far = Prediction(True, top_trigger_timestamp=label + timedelta(minutes=30), returned_any_trigger=True)
        assert trigger_hit(case, near) is True
        assert trigger_hit(case, far) is False
        # Returned no trigger at all.
        assert trigger_hit(case, Prediction(True)) is False
        # No trigger label.
        assert trigger_hit(_pos_case(), Prediction(True)) is None

    def test_negative_correct(self):
        assert negative_correct(_neg_case(), Prediction(produced_explanation=False)) is True
        assert negative_correct(_neg_case(), Prediction(produced_explanation=True)) is False
        assert negative_correct(_pos_case(), Prediction(True)) is None


# ── Baseline ──────────────────────────────────────────────────────────────────


class TestBaseline:
    def test_empty_window_abstains(self):
        pred = baseline_from_counts([])
        assert pred.produced_explanation is False
        assert pred.predicted_services == []

    def test_picks_most_frequent_service(self):
        rows = [("billing-worker", "fp1", 42), ("api", "fp2", 7)]
        pred = baseline_from_counts(rows)
        assert pred.produced_explanation is True
        assert pred.root_cause_service == "billing-worker"
        assert pred.returned_any_trigger is False


# ── Result normalization ──────────────────────────────────────────────────────


def _result(primary_services=None, triggers=None, confidence="high") -> ExplainResult:
    return ExplainResult(
        window_start=T0,
        window_end=T0 + timedelta(hours=1),
        summary_text="...",
        confidence=confidence,
        evidence_items=[],
        services_affected=primary_services or [],
        primary_cluster=({"services": primary_services} if primary_services is not None else None),
        trigger_candidates=triggers or [],
        total_logs=100,
    )


class TestPredictionFromResult:
    def test_produced_explanation_and_services(self):
        res = _result(
            primary_services=["billing-worker", "api"],
            triggers=[{"timestamp": (T0 + timedelta(minutes=15)).isoformat(), "service": "d", "message": "m"}],
        )
        pred = prediction_from_result(res)
        assert pred.produced_explanation is True
        assert pred.predicted_services == ["billing-worker", "api"]
        assert pred.returned_any_trigger is True
        assert pred.top_trigger_timestamp == T0 + timedelta(minutes=15)

    def test_none_primary_is_insufficient_evidence(self):
        pred = prediction_from_result(_result(primary_services=None))
        assert pred.produced_explanation is False
        assert pred.predicted_services == []
        assert pred.returned_any_trigger is False


# ── Report / lift ─────────────────────────────────────────────────────────────


class TestReport:
    def test_lift_and_calibration(self):
        label = T0 + timedelta(minutes=15)
        pos = _pos_case(trigger_ts=label)
        neg = _neg_case()
        results = [
            CaseResult(
                case=pos,
                raglogs=Prediction(
                    True,
                    predicted_services=["billing-worker"],
                    top_trigger_timestamp=label,
                    returned_any_trigger=True,
                    confidence="high",
                ),
                baseline=Prediction(True, predicted_services=["api"], confidence="low"),
            ),
            CaseResult(
                case=neg,
                raglogs=Prediction(produced_explanation=False, confidence="low"),
                baseline=Prediction(produced_explanation=False, confidence="low"),
            ),
        ]
        report = build_report(results)
        # raglogs got the one positive root cause; baseline missed it.
        assert report["raglogs"]["root_cause_accuracy"] == 1.0
        assert report["baseline"]["root_cause_accuracy"] == 0.0
        assert report["lift_over_baseline"]["root_cause_accuracy"] == 1.0
        assert report["raglogs"]["trigger_accuracy"] == 1.0
        assert report["raglogs"]["negative_precision"] == 1.0
        # Calibration: the "high" bucket holds the correct positive case.
        assert report["raglogs"]["calibration"]["high"]["accuracy"] == 1.0
        assert report["n_positive"] == 1
        assert report["n_negative"] == 1

    def test_scores_are_none_without_applicable_cases(self):
        # Only a negative case: no root-cause or trigger metrics apply.
        score = score_arm([(_neg_case(), Prediction(produced_explanation=False))])
        assert score.root_cause_accuracy is None
        assert score.trigger_accuracy is None
        assert score.negative_precision == 1.0
