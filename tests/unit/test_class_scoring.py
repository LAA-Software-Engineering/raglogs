"""Phase G (#185) — class scoring: the ranker as one signal, never a membership decider. Pure, no DB.

Covers the discrete evidence algebra (each strength × match/mismatch), UNKNOWN-neutrality (Invariant 1),
the max class aggregation, the ranker plugged in as a secondary ordering signal only (never moving a
class's rank above a higher discrete score, never changing membership — Invariant 6), and that scores
are discrete integers, not calibrated probabilities.
"""

import pytest
from src.core.rca.candidates import RootCauseCandidate
from src.core.rca.expectations import ExpectedObservation, ObservationModel, Strength
from src.core.rca.features import ServiceFeatures
from src.core.rca.hypothesis import Hypothesis, Kind, from_observation_model
from src.core.rca.observable import observed, unknown, uncollectable
from src.core.rca.partition import partition
from src.core.rca.scoring import (
    Support,
    rank_classes,
    score_class,
    score_hypothesis,
)


def _model(*expected: ExpectedObservation) -> ObservationModel:
    return ObservationModel(expected=expected)


def _h(hid: str, model: ObservationModel, *, localization: str = None, kind: Kind = Kind.PROCESS,
       score: float = 0.0) -> Hypothesis:
    cand = RootCauseCandidate(service=hid, score=score, features=ServiceFeatures(service=hid))
    return from_observation_model(hid, kind, localization or hid, model, source=cand)


class TestEvidenceAlgebra:
    @pytest.mark.parametrize("strength,expected_match,expected_mismatch", [
        (Strength.USUALLY, Support.STRONG_SUPPORT, Support.SOFT_CONTRADICTION),
        (Strength.OFTEN, Support.SUPPORT, Support.WEAK_CONTRADICTION),
        (Strength.MAYBE, Support.WEAK_SUPPORT, Support.WEAK_CONTRADICTION),
        (Strength.NOT_REQUIRED, Support.NEUTRAL, Support.NEUTRAL),
    ])
    def test_each_strength_match_and_mismatch(self, strength, expected_match, expected_mismatch):
        h = _h("h", _model(ExpectedObservation("a", "present", strength)))
        assert score_hypothesis(h, [observed("a", "present")]) == int(expected_match)
        assert score_hypothesis(h, [observed("a", "absent")]) == int(expected_mismatch)

    def test_score_sums_over_expectations(self):
        h = _h("h", _model(
            ExpectedObservation("a", "present", Strength.USUALLY),   # match  -> +3
            ExpectedObservation("b", "present", Strength.OFTEN),     # mismatch -> -1
        ))
        assert score_hypothesis(h, [observed("a", "present"), observed("b", "absent")]) == 2

    def test_hypothesis_without_model_scores_zero(self):
        h = Hypothesis(id="h", kind=Kind.PROCESS, localization="h", predictions={"a": "present"})
        assert score_hypothesis(h, [observed("a", "present")]) == 0

    def test_scores_are_not_probabilities(self):
        # discrete, can exceed 1 and go negative — never clamped to [0, 1].
        strong = _h("h", _model(
            ExpectedObservation("a", "present", Strength.USUALLY),
            ExpectedObservation("b", "present", Strength.USUALLY),
        ))
        assert score_hypothesis(strong, [observed("a", "present"), observed("b", "present")]) == 6
        neg = _h("h", _model(ExpectedObservation("a", "present", Strength.USUALLY)))
        assert score_hypothesis(neg, [observed("a", "absent")]) == -2


class TestUnknownNeutrality:
    def test_unobserved_expectation_is_neutral(self):
        h = _h("h", _model(ExpectedObservation("a", "present", Strength.USUALLY)))
        assert score_hypothesis(h, []) == 0

    def test_unknown_and_uncollectable_do_not_score(self):
        # Invariant 1/2: UNKNOWN and uncollectable coordinates contribute nothing, even if a match
        # would otherwise add weight.
        h = _h("h", _model(ExpectedObservation("a", "present", Strength.USUALLY)))
        assert score_hypothesis(h, [unknown("a"), uncollectable("b")]) == 0


class TestClassAggregation:
    def test_class_score_is_max_over_members(self):
        strong = _h("strong", _model(ExpectedObservation("a", "present", Strength.USUALLY)))
        weak = _h("weak", _model(ExpectedObservation("a", "present", Strength.MAYBE)))
        obs = [observed("a", "present")]
        assert score_class([weak, strong], obs) == 3  # max(+1, +3)

    def test_empty_class_rejected(self):
        with pytest.raises(ValueError):
            score_class([], [observed("a", "present")])


class TestRankClasses:
    def test_orders_by_discrete_score_desc(self):
        # two singleton classes (distinct signatures) -> UNCERTAIN partition; ranked by score.
        strong = _h("strong", _model(ExpectedObservation("a", "present", Strength.USUALLY)))
        weak = _h("weak", _model(ExpectedObservation("b", "present", Strength.MAYBE)))
        p = partition([weak, strong], [observed("a", "present"), observed("b", "present")])
        ranked = rank_classes(p)
        assert [r.hypothesis_ids for r in ranked] == [("strong",), ("weak",)]
        assert [r.score for r in ranked] == [3, 1]

    def test_ranker_is_only_a_tiebreak_never_overrides_score(self):
        # 'weak' has a much higher ranker score but a lower discrete score -> still ranked below.
        strong = _h("strong", _model(ExpectedObservation("a", "present", Strength.USUALLY)), score=0.1)
        weak = _h("weak", _model(ExpectedObservation("b", "present", Strength.MAYBE)), score=99.0)
        p = partition([weak, strong], [observed("a", "present"), observed("b", "present")])
        ranked = rank_classes(p)
        assert [r.hypothesis_ids for r in ranked] == [("strong",), ("weak",)]

    def test_ranker_breaks_ties_between_equal_scores(self):
        hi = _h("hi", _model(ExpectedObservation("a", "present", Strength.OFTEN)), score=5.0)
        lo = _h("lo", _model(ExpectedObservation("b", "present", Strength.OFTEN)), score=1.0)
        p = partition([lo, hi], [observed("a", "present"), observed("b", "present")])
        ranked = rank_classes(p)
        assert [r.hypothesis_ids for r in ranked] == [("hi",), ("lo",)]  # equal +2, ranker breaks it
        assert [r.score for r in ranked] == [2, 2]

    def test_use_ranker_false_drops_the_signal(self):
        h = _h("h", _model(ExpectedObservation("a", "present", Strength.USUALLY)), score=7.0)
        p = partition([h], [observed("a", "present")])
        (only,) = rank_classes(p, use_ranker=False)
        assert only.ranker_signal is None

    def test_scoring_does_not_move_the_partition(self):
        # Invariant 6: ranking is read-only — same classes/membership as the partition, just reordered.
        strong = _h("strong", _model(ExpectedObservation("a", "present", Strength.USUALLY)))
        weak = _h("weak", _model(ExpectedObservation("b", "present", Strength.MAYBE)))
        p = partition([weak, strong], [observed("a", "present"), observed("b", "present")])
        before = {frozenset(h.id for h in cls.members) for cls in p.classes}
        ranked = rank_classes(p)
        assert {frozenset(r.hypothesis_ids) for r in ranked} == before
        assert len(p.classes) == 2  # partition object untouched

    def test_member_breakdown_reported(self):
        strong = _h("strong", _model(ExpectedObservation("a", "present", Strength.USUALLY)))
        weak = _h("weak", _model(ExpectedObservation("a", "present", Strength.MAYBE)))
        # identical usable signature ({a: present}) -> one class with both members
        p = partition([strong, weak], [observed("a", "present")])
        (cls,) = rank_classes(p)
        assert dict(cls.member_scores) == {"strong": 3, "weak": 1}
        assert cls.score == 3  # max
