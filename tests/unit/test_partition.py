"""Phase D (#182) — structural partitioning.

Enforced as tests: F_usable excludes UNKNOWN/uncollectable and gates on confidence; equivalence is
equal usable categorical signatures and never score proximity (Inv 5); the partition is identical
under arbitrary ranking scores (Inv 6); nearby continuous values discretize into one class (Inv 7);
and D_missing names exactly the uncollected distinguishers.
"""

import random

import pytest
from src.core.rca.candidates import RootCauseCandidate
from src.core.rca.expectations import process_dead_model
from src.core.rca.features import ServiceFeatures
from src.core.rca.hypothesis import Hypothesis, Kind, from_observation_model
from src.core.rca.observable import State, observed, uncollectable, unknown
from src.core.rca.partition import (
    DEFAULT_POLICY,
    UsabilityPolicy,
    discretize_ratio,
    discretize_rate,
    partition,
    signature,
    usable_ids,
)


def _h(hid: str, predictions: dict, *, kind: Kind = Kind.PROCESS, score: float = 0.0) -> Hypothesis:
    cand = RootCauseCandidate(service=hid, score=score, features=ServiceFeatures(service=hid))
    return Hypothesis(id=hid, kind=kind, localization=hid, predictions=predictions, _source=cand)


class TestFUsable:
    def test_excludes_unknown_and_uncollectable(self):
        obs = [observed("a", "present"), unknown("b"), uncollectable("c")]
        assert usable_ids(obs) == ("a",)

    def test_confidence_threshold_gates(self):
        obs = [observed("a", "present", measurement_confidence=0.9),
               observed("d", "present", measurement_confidence=0.2)]
        assert usable_ids(obs) == ("a", "d")                       # default tau 0.0 accepts both
        assert usable_ids(obs, UsabilityPolicy(default_tau=0.5)) == ("a",)
        assert usable_ids(obs, UsabilityPolicy(tau={"a": 0.95})) == ("d",)

    def test_signature_projects_categorical_predictions_over_f_usable(self):
        f = ("a", "d")
        assert signature({"a": "present", "d": "high", "z": "absent"}, f) == (
            ("a", "present"), ("d", "high"),
        )


class TestPartitionBasics:
    def test_equal_usable_signatures_share_a_class(self):
        obs = [observed("a", "present"), observed("b", "present")]
        h1 = _h("h1", {"a": "present", "b": "present"})
        h2 = _h("h2", {"a": "present", "b": "present"})
        h3 = _h("h3", {"a": "absent"}, kind=Kind.EDGE)
        p = partition([h1, h2, h3], obs)
        assert len(p.classes) == 2
        by_size = sorted(p.classes, key=lambda c: len(c.members))
        assert by_size[1].members == (h1, h2)  # sorted by id
        assert by_size[0].is_singleton

    def test_deterministic_ordering(self):
        obs = [observed("a", "present")]
        hs = [_h("z", {"a": "absent"}), _h("a", {"a": "present"}), _h("m", {"a": "present"})]
        p = partition(hs, obs)
        assert [c.signature for c in p.classes] == sorted(c.signature for c in p.classes)

    def test_hard_incompatibility_filters_before_partitioning(self):
        m = process_dead_model("payment")
        dead = from_observation_model("process:payment", Kind.PROCESS, "payment", m)
        other = _h("process:other", {"a": "present"})
        obs = [observed("payment.serving_throughout_window", "true"), observed("a", "present")]
        p = partition([dead, other], obs)
        assert dead in p.eliminated
        assert all(dead not in c.members for c in p.classes)


class TestInvariant5EquivalenceNotScoreProximity:
    def test_same_signature_far_apart_scores_still_one_class(self):
        obs = [observed("a", "present")]
        p = partition([_h("h1", {"a": "present"}, score=0.01),
                       _h("h2", {"a": "present"}, score=999.0)], obs)
        assert len(p.classes) == 1 and len(p.classes[0].members) == 2

    def test_different_signature_identical_scores_stay_separate(self):
        obs = [observed("a", "present")]
        p = partition([_h("h1", {"a": "present"}, score=5.0),
                       _h("h2", {"a": "absent"}, score=5.0)], obs)
        assert len(p.classes) == 2


class TestInvariant6PartitionInvariantUnderScores:
    def test_random_score_vectors_give_identical_partitions(self):
        obs = [observed("a", "present"), observed("b", "present"), unknown("c")]
        preds = [{"a": "present", "b": "present", "c": "present"},
                 {"a": "present", "b": "present", "c": "absent"},
                 {"a": "absent"}]
        rng = random.Random(1234)
        base = partition([_h(f"h{i}", p, score=0.0) for i, p in enumerate(preds)], obs)
        for _ in range(20):
            scored = [_h(f"h{i}", p, score=rng.uniform(-1e6, 1e6)) for i, p in enumerate(preds)]
            assert partition(scored, obs) == base


class TestInvariant7ContinuousDifferencesDoNotSplit:
    def test_discretizers_bucket_nearby_values_together(self):
        assert discretize_rate(0.11) == discretize_rate(0.13) == State.PRESENT
        assert discretize_ratio(1.05) == discretize_ratio(1.10) == State.NORMAL
        assert discretize_ratio(2.0) == State.HIGH and discretize_ratio(0.4) == State.LOW

    def test_close_expected_rates_land_in_one_class(self):
        obs = [observed("err", "present")]
        h1 = _h("h1", {"err": discretize_rate(0.11)})
        h2 = _h("h2", {"err": discretize_rate(0.13)})
        p = partition([h1, h2], obs)
        assert len(p.classes) == 1 and len(p.classes[0].members) == 2


class TestDMissing:
    def test_names_the_uncollected_distinguisher(self):
        # both agree on the usable 'a'; they differ only on 'b', which is UNKNOWN (not usable)
        obs = [observed("a", "present"), unknown("b")]
        h1 = _h("h1", {"a": "present", "b": "present"})
        h2 = _h("h2", {"a": "present", "b": "absent"})
        p = partition([h1, h2], obs)
        assert len(p.classes) == 1
        assert p.classes[0].d_missing == frozenset({"b"})

    def test_singleton_class_has_empty_d_missing(self):
        obs = [observed("a", "present")]
        p = partition([_h("h1", {"a": "present"})], obs)
        assert p.classes[0].is_singleton
        assert p.classes[0].d_missing == frozenset()

    def test_usable_distinguisher_separates_instead_of_missing(self):
        # when the distinguisher 'b' IS usable, the two land in different classes and nothing missing
        obs = [observed("a", "present"), observed("b", "present")]
        h1 = _h("h1", {"a": "present", "b": "present"})
        h2 = _h("h2", {"a": "present", "b": "absent"})
        p = partition([h1, h2], obs)
        assert len(p.classes) == 2
        assert all(c.d_missing == frozenset() for c in p.classes)

    def test_below_threshold_coordinate_becomes_missing(self):
        # 'b' is OBSERVED but below its confidence threshold -> not usable -> a missing distinguisher
        obs = [observed("a", "present"), observed("b", "present", measurement_confidence=0.1)]
        policy = UsabilityPolicy(tau={"b": 0.8})
        h1 = _h("h1", {"a": "present", "b": "present"})
        h2 = _h("h2", {"a": "present", "b": "absent"})
        p = partition([h1, h2], obs, policy)
        assert len(p.classes) == 1
        assert p.classes[0].d_missing == frozenset({"b"})


def test_default_policy_is_shared_singleton():
    assert DEFAULT_POLICY.default_tau == 0.0
    with pytest.raises(TypeError):
        DEFAULT_POLICY.tau["x"] = 1.0  # type: ignore[index]  # read-only view
