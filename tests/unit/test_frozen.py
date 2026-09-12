"""Unit tests for the frozen external-validation scorer (#79). No DB.

These test *plumbing and metric math only* — never used to tune the scorer or the
frozen model/calibrator (that would turn external validation into training)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.core.explain.summarizer import ExplainResult
from src.eval.case import EvalCase, RootCause, Trigger
from src.eval.frozen import (
    FrozenCaseResult,
    ece,
    frozen_case_result,
    provenance_header,
    render_frozen_report,
    score_frozen,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _case(cid, *, service, trigger_type, expect=True, notes="", trigger_ts=T0):
    return EvalCase(
        id=cid, window_start=T0, window_end=T0 + timedelta(minutes=10), expect_explanation=expect,
        root_cause=RootCause(service=service) if service else None,
        trigger=Trigger(timestamp=trigger_ts, type=trigger_type) if trigger_type else None,
        notes=notes,
    )


def _result(*, candidates=None, confidence=None, primary=None, triggers=None):
    return ExplainResult(
        window_start=T0, window_end=T0, summary_text="", confidence="low",
        evidence_items=[], services_affected=[], primary_cluster=primary,
        trigger_candidates=triggers or [],
        predicted_root_cause=(candidates[0]["service"] if candidates else None),
        root_cause_candidates=candidates or [],
        predicted_root_cause_confidence=confidence,
    )


class TestFrozenCaseResult:
    def test_top1_top3_and_confidence(self):
        case = _case("c1", service="payment", trigger_type="code")
        res = _result(
            candidates=[{"service": "frontend"}, {"service": "payment"}, {"service": "cart"}],
            confidence=0.7,
        )
        fr = frozen_case_result(case, res)
        assert fr.correct_top1 is False  # payment is 2nd
        assert fr.correct_top3 is True
        assert fr.confidence == 0.7
        assert fr.fault_class == "code" and fr.produced is True

    def test_trigger_correct_within_tolerance(self):
        case = _case("c2", service="cart", trigger_type="deploy", trigger_ts=T0)
        near = (T0 + timedelta(minutes=2)).isoformat()
        far = (T0 + timedelta(minutes=30)).isoformat()
        assert frozen_case_result(case, _result(triggers=[{"timestamp": near}])).trigger_correct is True
        assert frozen_case_result(case, _result(triggers=[{"timestamp": far}])).trigger_correct is False

    def test_negative_and_confounded_flags(self):
        neg = frozen_case_result(_case("n1", service=None, trigger_type=None, expect=False), _result())
        assert neg.is_negative is True and neg.produced is False
        conf = frozen_case_result(
            _case("x1", service="cart", trigger_type="code", notes="confounder: unrelated deploy at ..."),
            _result(candidates=[{"service": "cart"}]),
        )
        assert conf.is_confounded is True

    def test_modalities_from_case_paths(self, tmp_path):
        case = _case("m1", service="cart", trigger_type="code")
        case.spans_path = tmp_path / "spans.jsonl"
        assert frozen_case_result(case, _result()).modalities == "logs+traces"


def _fr(**kw):
    base = dict(case_id="c", fault_class="code", is_negative=False, is_confounded=False,
                truth_service="cart", ranked_services=["cart"], confidence=None,
                produced=True, trigger_correct=None, modalities="logs")
    base.update(kw)
    return FrozenCaseResult(**base)


class TestScoreFrozen:
    def test_top1_top3_abstention_and_per_class(self):
        results = [
            _fr(fault_class="code", ranked_services=["cart"]),  # top1 hit
            _fr(fault_class="deploy", truth_service="pay", ranked_services=["x", "pay"], trigger_correct=True),  # top3 only
            _fr(is_negative=True, truth_service=None, produced=False),  # correct abstention
            _fr(is_negative=True, truth_service=None, produced=True),  # false alarm
        ]
        r = score_frozen(results)
        assert r["n_positive"] == 2 and r["n_negative"] == 2
        assert r["top1"] == pytest.approx(0.5)
        assert r["top3"] == pytest.approx(1.0)
        assert r["negative_abstention"] == pytest.approx(0.5)
        assert r["deploy_trigger_correct"] == pytest.approx(1.0)
        assert r["per_fault_class"]["code"]["top1"] == pytest.approx(1.0)
        assert r["per_fault_class"]["deploy"]["top1"] == pytest.approx(0.0)

    def test_per_case_detail_is_emitted(self):
        results = [
            _fr(case_id="c1", ranked_services=["cart", "web", "db"]),
            _fr(case_id="neg", is_negative=True, truth_service=None, produced=False),
        ]
        cases = score_frozen(results)["cases"]
        assert [c["id"] for c in cases] == ["c1", "neg"]
        assert cases[0]["truth_service"] == "cart" and cases[0]["correct_top1"] is True
        assert cases[0]["predicted_services"] == ["cart", "web", "db"]  # top-3
        assert cases[1]["is_negative"] is True and cases[1]["produced"] is False

    def test_gate_absent_when_not_evaluated(self):
        # no abstained field set -> gate view omitted, ranker metrics unaffected
        assert score_frozen([_fr(ranked_services=["cart"])])["gate"] is None

    def test_gate_and_end_to_end_views(self):
        results = [
            _fr(case_id="p1", ranked_services=["cart"], abstained=False),  # hit, proceed
            _fr(case_id="p2", truth_service="pay", ranked_services=["x"], abstained=True),  # miss, wrongly abstained
            _fr(case_id="n1", is_negative=True, truth_service=None, abstained=True),   # correct abstain
            _fr(case_id="n2", is_negative=True, truth_service=None, abstained=False),  # false alarm
        ]
        r = score_frozen(results)
        # RANKER is scored over ALL positives, independent of the gate: p2 (abstained)
        # still counts, so top-1 = 1/2 — a good gate can't hide the bad ranker case.
        assert r["top1"] == pytest.approx(0.5)
        g = r["gate"]
        assert g["incident_recall"] == pytest.approx(0.5)      # p1 proceeded of 2
        assert g["healthy_abstention"] == pytest.approx(0.5)   # n1 abstained of 2
        assert g["coverage"] == pytest.approx(0.5)             # p1,n2 proceeded of 4
        assert g["selective_top1"] == pytest.approx(1.0)       # only p1 covered, and it's a hit
        assert g["false_diagnosis_rate"] == pytest.approx(0.5)  # n2 proceeded on healthy
        by = {c["id"]: c for c in r["cases"]}
        assert by["p2"]["abstained"] is True and by["p1"]["abstained"] is False

    def test_confounded_and_modality_breakdown(self):
        results = [
            _fr(is_confounded=True, trigger_correct=True, modalities="logs+metrics"),
            _fr(is_confounded=True, trigger_correct=False, modalities="logs+metrics", ranked_services=["cart"]),
        ]
        r = score_frozen(results)
        assert r["confounded_trigger_correct"] == pytest.approx(0.5)
        assert r["per_modality"]["logs+metrics"]["n"] == 2

    def test_confidence_ece(self):
        results = [
            _fr(confidence=0.9, ranked_services=["cart"]),  # conf .9, correct
            _fr(confidence=0.9, ranked_services=["x"]),      # conf .9, wrong -> bin acc .5
        ]
        r = score_frozen(results)
        assert r["confidence_ece"] == pytest.approx(0.4, abs=1e-9)  # |0.5-0.9|*1.0


class TestReport:
    def test_provenance_header_states_zero_training(self):
        hdr = "\n".join(provenance_header(ranker_path="r.json", calibrator_path="c.json"))
        assert "OTel cases seen in training: 0" in hdr
        assert "Model changes after capture: 0" in hdr
        assert "frozen external validation" in hdr

    def test_render_includes_metrics_and_header(self):
        report = score_frozen([_fr(ranked_services=["cart"])])
        text = render_frozen_report(report, ranker_path="r.json", calibrator_path="c.json")
        assert "frozen external validation" in text
        assert "root-cause top-1" in text
        assert "per fault class" in text


class TestEce:
    def test_empty(self):
        assert ece([]) == 0.0
