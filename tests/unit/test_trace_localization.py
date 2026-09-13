"""Unit tests for the trace-localization benchmark scorer + label schema (#118). No DB."""
import pytest

from src.eval.trace_localization import (
    LocResult,
    TraceLocCase,
    load_trace_loc_labels,
    score_localization,
)


def _case(**kw):
    base = dict(id="c", root_cause="payment", fault_type="callee_fail",
                first_failing="payment", propagation_path=["payment", "checkout", "frontend"],
                symptom_services=["checkout"], edges=[("checkout", "payment")])
    base.update(kw)
    return TraceLocCase(**base)


class TestLocResult:
    def test_top1_top3(self):
        c = _case()
        assert LocResult(["payment", "checkout"]).top1_correct(c) is True
        assert LocResult(["checkout", "payment"]).top1_correct(c) is False
        assert LocResult(["checkout", "cart", "payment"]).top3_correct(c) is True
        assert LocResult(["a", "b", "c", "payment"]).top3_correct(c) is False

    def test_cause_above_symptom(self):
        c = _case()  # symptom = checkout
        assert LocResult(["payment", "checkout"]).cause_above_symptom(c) is True
        assert LocResult(["checkout", "payment"]).cause_above_symptom(c) is False

    def test_cause_above_symptom_none_when_no_distinct_symptom(self):
        c = _case(symptom_services=[])
        assert LocResult(["payment"]).cause_above_symptom(c) is None

    def test_cause_above_symptom_false_when_cause_unranked(self):
        # the candidate-generation miss: cause isn't even a candidate
        c = _case()
        assert LocResult(["checkout", "cart"]).cause_above_symptom(c) is False

    def test_symptom_absent_from_ranking_does_not_outrank(self):
        c = _case()  # only cause ranked, symptom not a candidate
        assert LocResult(["payment"]).cause_above_symptom(c) is True

    def test_first_failing(self):
        c = _case()
        assert LocResult(["payment"], first_failing_pred="payment").first_failing_correct(c) is True
        assert LocResult(["payment"], first_failing_pred="checkout").first_failing_correct(c) is False
        assert LocResult(["payment"]).first_failing_correct(c) is None


class TestScore:
    def test_aggregate_and_per_fault(self):
        pairs = [
            (_case(fault_type="callee_fail"), LocResult(["payment", "checkout"])),   # top1 + cas
            (_case(fault_type="symptom_only"), LocResult(["checkout", "payment"])),  # miss + cas False
            (_case(fault_type="caller_fail", symptom_services=[]), LocResult(["payment"])),  # cas None
        ]
        r = score_localization(pairs)
        assert r["n"] == 3
        assert r["top1"] == pytest.approx(2 / 3)
        # cause_above_symptom only over the 2 cases with a distinct symptom -> 1/2
        assert r["cause_above_symptom"] == pytest.approx(0.5)
        assert r["per_fault_type"]["callee_fail"]["top1"] == pytest.approx(1.0)
        assert r["per_fault_type"]["symptom_only"]["cause_above_symptom"] == pytest.approx(0.0)

    def test_first_failing_rate_ignores_none(self):
        pairs = [
            (_case(), LocResult(["payment"], first_failing_pred="payment")),
            (_case(), LocResult(["payment"])),  # None -> ignored
        ]
        assert score_localization(pairs)["first_failing"] == pytest.approx(1.0)


class TestLabelLoader:
    def test_loads_block(self, tmp_path):
        (tmp_path / "case.yaml").write_text(
            "id: c1\n"
            "trace_localization:\n"
            "  root_cause: payment\n"
            "  fault_type: symptom_only\n"
            "  first_failing: payment\n"
            "  propagation_path: [payment, checkout]\n"
            "  symptom_services: [checkout]\n"
            "  edges: [[checkout, payment]]\n"
        )
        c = load_trace_loc_labels(tmp_path / "case.yaml")
        assert c.root_cause == "payment" and c.fault_type == "symptom_only"
        assert c.edges == [("checkout", "payment")] and c.has_distinct_symptom

    def test_none_when_no_block(self, tmp_path):
        (tmp_path / "case.yaml").write_text("id: c1\nroot_cause:\n  service: x\n")
        assert load_trace_loc_labels(tmp_path / "case.yaml") is None

    def test_rejects_bad_fault_type(self, tmp_path):
        (tmp_path / "case.yaml").write_text(
            "trace_localization:\n  root_cause: p\n  fault_type: bogus\n"
        )
        with pytest.raises(ValueError):
            load_trace_loc_labels(tmp_path / "case.yaml")
