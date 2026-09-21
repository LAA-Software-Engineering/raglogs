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


def _pred(*, produced: bool = True, top: str | None = None, services=(), generated=None) -> Prediction:
    return Prediction(produced_explanation=produced, root_cause_service=top,
                      predicted_services=list(services),
                      generated_candidates=list(services if generated is None else generated))


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

    def test_inference_when_generated_but_not_in_selected_topk(self):
        # db was GENERATED (in the full set) but selected/top-k output only has api -> inference, not
        # coverage. This is the exact secondary-cluster / beyond-top-k case.
        p = _pred(top="api", services=["api"], generated=["api", "db"])
        assert classify_failure(_case("c", service="db"), p) is Bucket.INFERENCE

    def test_coverage_only_when_absent_from_the_full_generated_set(self):
        p = _pred(top="api", services=["api"], generated=["api", "cache"])  # db truly never generated
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

    def test_counts_shares_and_failure_composition(self):
        tax = build_taxonomy(self._pairs())
        assert tax.n_scored == 4 and tax.n_failures == 3  # the negative case is excluded
        assert tax.counts == {"correct": 1, "inference": 1, "coverage": 1, "detection": 1}
        assert tax.share_of_scored(Bucket.COVERAGE) == 0.25       # of all scored
        assert tax.share_of_failures(Bucket.COVERAGE) == 1 / 3    # of failures only
        assert tax.share_of_failures(Bucket.CORRECT) == 0.0
        assert tax.failure_rate == 0.75
        assert tax.case_ids["coverage"] == ["c3"]

    def test_mostly_correct_population_composition(self):
        # 90 correct / 5 coverage / 5 inference: 5% each of scored, but 50/50 of FAILURES
        pairs = ([(_case(f"ok{i}", service="db"), _pred(top="db", services=["db"])) for i in range(90)]
                 + [(_case(f"cov{i}", service="db"), _pred(top="api", generated=["api"])) for i in range(5)]
                 + [(_case(f"inf{i}", service="db"), _pred(top="api", generated=["api", "db"])) for i in range(5)])
        tax = build_taxonomy(pairs)
        assert tax.failure_rate == 0.10
        assert tax.share_of_scored(Bucket.COVERAGE) == 0.05
        assert tax.share_of_failures(Bucket.COVERAGE) == 0.5
        assert tax.share_of_failures(Bucket.INFERENCE) == 0.5

    def test_empty_is_safe(self):
        tax = build_taxonomy([])
        assert tax.n_scored == 0 and tax.n_failures == 0
        assert tax.share_of_scored(Bucket.CORRECT) is None
        assert tax.share_of_failures(Bucket.COVERAGE) is None
        assert tax.failure_rate is None


class TestReportContract:
    def _results(self):
        from src.eval.runner import CaseResult
        base = _pred(produced=False)
        mk = lambda cid, rag: CaseResult(case=_case(cid, service="db"), raglogs=rag, baseline=base)  # noqa: E731
        return ([mk(f"ok{i}", _pred(top="db", services=["db"])) for i in range(8)]
                + [mk("cov", _pred(top="api", generated=["api"]))]
                + [mk("inf", _pred(top="api", generated=["api", "db"]))])

    def test_build_report_publishes_both_distributions(self):
        from src.eval.report import build_report
        tax = build_report(self._results())["failure_taxonomy"]
        assert tax["n_scored"] == 10 and tax["n_failures"] == 2
        assert tax["failure_rate"] == 0.2
        assert tax["share_of_scored"]["coverage"] == 0.1     # of all scored
        assert tax["share_of_failures"]["coverage"] == 0.5   # of failures
        assert tax["share_of_failures"]["inference"] == 0.5
        assert tax["case_ids"]["coverage"] == ["cov"]

    def test_render_table_shows_failure_composition(self):
        from src.eval.report import build_report, render_table
        table = render_table(build_report(self._results()))
        assert "Failure taxonomy" in table and "of failures" in table


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
