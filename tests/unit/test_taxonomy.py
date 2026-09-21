"""Phase H1 (#186) — the existing-pipeline failure taxonomy. Pure, no DB."""

from datetime import datetime

from src.eval.case import EvalCase, RootCause
from src.eval.metrics import Prediction
from src.eval.taxonomy import (
    SCORABLE_AXES,
    Bucket,
    build_taxonomy,
    classify_failure,
)

_T0 = datetime(2026, 1, 1)


def _case(cid: str, *, service: str | None = "svc", expect: bool = True) -> EvalCase:
    return EvalCase(
        id=cid, window_start=_T0, window_end=_T0, expect_explanation=expect,
        root_cause=RootCause(service=service) if service else None,
    )


def _pred(*, produced: bool = True, top: str | None = None, services=()) -> Prediction:
    return Prediction(produced_explanation=produced, root_cause_service=top,
                      predicted_services=list(services))


class TestClassifyFailure:
    def test_correct_is_top1_hit(self):
        p = _pred(top="db", services=["db", "api"])
        assert classify_failure(_case("c", service="db"), p) is Bucket.CORRECT

    def test_inference_when_present_but_not_top1(self):
        p = _pred(top="api", services=["api", "db"])  # db is a candidate, but ranked below api
        assert classify_failure(_case("c", service="db"), p) is Bucket.INFERENCE

    def test_coverage_when_absent_from_candidates(self):
        p = _pred(top="api", services=["api", "cache"])  # db never generated
        assert classify_failure(_case("c", service="db"), p) is Bucket.COVERAGE

    def test_detection_when_no_explanation(self):
        assert classify_failure(_case("c", service="db"), _pred(produced=False)) is Bucket.DETECTION

    def test_out_of_scope_cases_return_none(self):
        assert classify_failure(_case("c", expect=False), _pred(top="db")) is None       # negative
        assert classify_failure(_case("c", service=None), _pred(top="db")) is None        # unlabeled


class TestBuildTaxonomy:
    def _pairs(self):
        return [
            (_case("c1", service="db"), _pred(top="db", services=["db"])),              # CORRECT
            (_case("c2", service="db"), _pred(top="api", services=["api", "db"])),      # INFERENCE
            (_case("c3", service="db"), _pred(top="api", services=["api"])),            # COVERAGE
            (_case("c4", service="db"), _pred(produced=False)),                          # DETECTION
            (_case("c5", expect=False), _pred(produced=False)),                          # out of scope
        ]

    def test_counts_and_shares(self):
        tax = build_taxonomy(self._pairs())
        assert tax.n_scored == 4  # the negative case is excluded
        assert tax.counts == {"correct": 1, "inference": 1, "coverage": 1, "detection": 1}
        assert tax.share(Bucket.COVERAGE) == 0.25
        assert tax.failure_share == 0.75  # everything but CORRECT
        assert tax.case_ids["coverage"] == ["c3"]

    def test_empty_is_safe(self):
        tax = build_taxonomy([])
        assert tax.n_scored == 0
        assert tax.share(Bucket.CORRECT) is None
        assert tax.failure_share is None


class TestScorableAxes:
    def test_existing_pipeline_axes_are_marked_existing(self):
        for axis in ("detection", "coverage", "inference"):
            assert SCORABLE_AXES[axis] == "existing"

    def test_structural_axes_need_phase_h2(self):
        for axis in ("observability", "ontology", "observation_model", "non_identifiable",
                     "structural_outcome"):
            assert SCORABLE_AXES[axis] == "structural"

    def test_d_missing_needs_availability_ground_truth(self):
        assert SCORABLE_AXES["d_missing_correctness"] == "avail-gt"
