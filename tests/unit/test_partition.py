"""Phase D (#182) — structural partitioning.

Enforced as tests: F_usable excludes UNKNOWN/uncollectable and gates on confidence; equivalence is
equal usable categorical signatures and never score proximity (Inv 5); the partition is identical
under arbitrary ranking scores (Inv 6); nearby continuous values discretize into one class (Inv 7);
and D_missing names exactly the uncollected distinguishers.
"""

import random

import pytest
from src.core.rca.candidates import RootCauseCandidate
from src.core.rca.expectations import (
    Contradiction,
    ExpectedObservation,
    ObservationModel,
    process_dead_model,
)
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
        # predictions only, over F_usable; ids not predicted or not usable are omitted
        assert signature({"a": "present", "d": "high", "z": "absent"}, ("a", "d")) == (
            ("a", "present"), ("d", "high"),
        )


class TestPartitionBasics:
    def test_equal_usable_signatures_share_a_class(self):
        obs = [observed("a", "present"), observed("b", "present")]
        # h1/h2 agree on the usable signature but differ on the unobserved 'hidden' (non-degenerate)
        h1 = _h("h1", {"a": "present", "b": "present", "hidden": "present"})
        h2 = _h("h2", {"a": "present", "b": "present", "hidden": "absent"})
        h3 = _h("h3", {"a": "absent"}, kind=Kind.EDGE)
        p = partition([h1, h2, h3], obs)
        assert len(p.classes) == 2
        by_size = sorted(p.classes, key=lambda c: len(c.members))
        assert by_size[1].members == (h1, h2)  # sorted by id
        assert by_size[0].is_singleton

    def test_deterministic_ordering(self):
        obs = [observed("a", "present")]
        hs = [_h("z", {"a": "absent"}), _h("a", {"a": "present", "hidden": "p"}),
              _h("m", {"a": "present", "hidden": "q"})]
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

    def test_below_threshold_observation_cannot_eliminate(self):
        # the same measurement that is too untrustworthy for a signature must not irreversibly kill a
        # hypothesis: hard elimination consumes the SAME confidence-gated usable set.
        m = process_dead_model("payment")
        dead = from_observation_model("process:payment", Kind.PROCESS, "payment", m)
        coord = "payment.serving_throughout_window"
        obs = [observed(coord, "true", measurement_confidence=0.1)]
        policy = UsabilityPolicy(tau={coord: 0.8})
        p = partition([dead], obs, policy)
        assert dead not in p.eliminated
        assert coord not in p.f_usable        # excluded from F_usable...
        assert dead in p.classes[0].members   # ...and therefore did not eliminate

    def test_above_threshold_observation_still_eliminates(self):
        m = process_dead_model("payment")
        dead = from_observation_model("process:payment", Kind.PROCESS, "payment", m)
        coord = "payment.serving_throughout_window"
        obs = [observed(coord, "true", measurement_confidence=0.9)]
        policy = UsabilityPolicy(tau={coord: 0.8})
        assert dead in partition([dead], obs, policy).eliminated


class TestInputBoundaryIsASet:
    def test_exact_duplicates_do_not_change_cardinality(self):
        obs = [observed("a", "present")]
        h = _h("h1", {"a": "present"})
        single = partition([h], obs)
        dup = partition([h, h], obs)
        assert dup == single
        assert dup.classes[0].is_singleton and dup.classes[0].d_missing == frozenset()

    def test_conflicting_definitions_for_one_id_are_rejected(self):
        obs = [observed("a", "present")]
        h1 = _h("dup", {"a": "present"})
        h2 = _h("dup", {"a": "absent"})  # same id, different behavior
        with pytest.raises(ValueError):
            partition([h1, h2], obs)

    def test_partition_is_permutation_invariant(self):
        obs = [observed("a", "present")]
        hs = [_h("h1", {"a": "present", "hid": "p"}), _h("h2", {"a": "absent"}),
              _h("h3", {"a": "present", "hid": "q"})]
        assert partition(hs, obs) == partition(list(reversed(hs)), obs)

    def test_permutation_invariant_including_eliminated(self):
        a = from_observation_model("process:a", Kind.PROCESS, "a", process_dead_model("svca"))
        b = from_observation_model("process:b", Kind.PROCESS, "b", process_dead_model("svcb"))
        obs = [observed("svca.serving_throughout_window", "true"),
               observed("svcb.serving_throughout_window", "true")]
        assert partition([a, b], obs) == partition([b, a], obs)
        assert [h.id for h in partition([b, a], obs).eliminated] == ["process:a", "process:b"]

    def test_distinct_ids_with_equal_predictions_are_grouped_not_rejected(self):
        # an equivalence relation groups distinct-but-equivalent elements — this is the honest
        # non-identifiability the partition represents, not a malformed input.
        m = ObservationModel(expected=(ExpectedObservation("f", "present"),))
        a = from_observation_model("h1", Kind.PROCESS, "a", m)
        b = from_observation_model("h2", Kind.PROCESS, "b", m)  # distinct id, identical predictions
        p = partition([a, b], [observed("f", "present")])
        assert len(p.classes) == 1
        assert len(p.classes[0].members) == 2
        assert p.classes[0].d_missing == frozenset()  # irreducible: no prediction distinguisher

    def test_same_id_divergent_provenance_is_rejected(self):
        # Hypothesis.__eq__ ignores source, so silently keeping the first copy would make the score
        # reaching Phase G input-order-dependent. Reject instead.
        obs = [observed("a", "present")]
        h_lo = _h("dup", {"a": "present"}, score=0.1)
        h_hi = _h("dup", {"a": "present"}, score=0.9)
        with pytest.raises(ValueError):
            partition([h_lo, h_hi], obs)

    def test_empty_hypothesis_set_is_rejected(self):
        with pytest.raises(ValueError):
            partition([], [observed("a", "present")])

    def test_all_eliminated_is_the_explicit_no_survivor_state(self):
        a = from_observation_model("process:a", Kind.PROCESS, "a", process_dead_model("svca"))
        b = from_observation_model("process:b", Kind.PROCESS, "b", process_dead_model("svcb"))
        obs = [observed("svca.serving_throughout_window", "true"),
               observed("svcb.serving_throughout_window", "true")]
        p = partition([a, b], obs)
        assert p.classes == ()
        assert p.no_surviving_hypothesis is True
        assert [h.id for h in p.eliminated] == ["process:a", "process:b"]

    def test_hollow_partition_cannot_masquerade_as_no_survivor(self):
        from src.core.rca.partition import Partition
        # the value type enforces its own invariant, independent of the factory
        with pytest.raises(ValueError):
            Partition(classes=(), f_usable=(), eliminated=())

    def test_empty_equivalence_class_is_rejected(self):
        from src.core.rca.partition import EquivalenceClass, Partition
        with pytest.raises(ValueError):
            EquivalenceClass(signature=(), members=(), d_missing=frozenset())
        # ...so the nested hollow partition the reviewer flagged cannot be built either
        with pytest.raises(ValueError):
            Partition(classes=(EquivalenceClass(signature=(), members=(), d_missing=frozenset()),),
                      f_usable=(), eliminated=())


class TestNonFiniteInputsRejected:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1])
    def test_ratio_rejects_non_finite_and_negative(self, bad):
        with pytest.raises(ValueError):
            discretize_ratio(bad)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.5])
    def test_rate_rejects_non_finite_and_out_of_range(self, bad):
        with pytest.raises(ValueError):
            discretize_rate(bad)

    def test_ratio_rejects_incoherent_thresholds(self):
        with pytest.raises(ValueError):
            discretize_ratio(1.0, high=0.5, low=2.0)

    def test_booleans_are_not_valid_magnitudes(self):
        with pytest.raises(ValueError):
            discretize_rate(True)  # type: ignore[arg-type]


class TestPolicyValidation:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), 1.5, -0.1])
    def test_default_tau_must_be_a_confidence(self, bad):
        with pytest.raises(ValueError):
            UsabilityPolicy(default_tau=bad)

    def test_override_thresholds_are_validated(self):
        with pytest.raises(ValueError):
            UsabilityPolicy(tau={"a": float("nan")})
        with pytest.raises(ValueError):
            UsabilityPolicy(tau={"a": 2.0})

    def test_override_ids_must_be_non_empty_strings(self):
        with pytest.raises(ValueError):
            UsabilityPolicy(tau={"": 0.5})


class TestInvariant5EquivalenceNotScoreProximity:
    def test_same_signature_far_apart_scores_still_one_class(self):
        obs = [observed("a", "present")]
        p = partition([_h("h1", {"a": "present", "hid": "p"}, score=0.01),
                       _h("h2", {"a": "present", "hid": "q"}, score=999.0)], obs)
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
        # the only usable coordinate 'err' discretizes to the same band from 0.11 and 0.13, so it
        # does not split the two into distinct classes; they differ only on the unobserved 'hid'.
        h1 = _h("h1", {"err": discretize_rate(0.11), "hid": "p"})
        h2 = _h("h2", {"err": discretize_rate(0.13), "hid": "q"})
        p = partition([h1, h2], obs)
        assert len(p.classes) == 1 and len(p.classes[0].members) == 2
        assert "err" not in p.classes[0].d_missing  # the rate coordinate was not a distinguisher


class TestDMissing:
    def test_names_the_uncollected_distinguisher(self):
        # both agree on the usable 'a'; they differ only on 'b', which is UNKNOWN (not usable)
        obs = [observed("a", "present"), unknown("b")]
        h1 = _h("h1", {"a": "present", "b": "present"})
        h2 = _h("h2", {"a": "present", "b": "absent"})
        p = partition([h1, h2], obs)
        assert len(p.classes) == 1
        assert p.classes[0].d_missing == frozenset({"b"})

    def test_hard_rules_do_not_enter_the_relation_or_d_missing(self):
        # #182 keeps ~_O prediction-only: a hard-rule difference (only h1 contradicts g) is NOT a
        # prediction distinguisher. Same predictions -> one class; g is absent from d_missing whether
        # g is UNKNOWN or usable. (Hard-rule discriminators are surfaced separately by Phase E.)
        m1 = ObservationModel(expected=(ExpectedObservation("f", "present"),),
                              contradictions=(Contradiction("g", frozenset({"true"})),))
        m2 = ObservationModel(expected=(ExpectedObservation("f", "present"),))
        h1 = from_observation_model("h1", Kind.PROCESS, "h1", m1)
        h2 = from_observation_model("h2", Kind.PROCESS, "h2", m2)
        for gobs in (unknown("g"), observed("g", "false")):  # g unavailable, or usable-but-false
            p = partition([h1, h2], [observed("f", "present"), gobs])
            assert len(p.classes) == 1
            assert len(p.classes[0].members) == 2
            assert "g" not in p.classes[0].d_missing
            assert p.classes[0].d_missing == frozenset()  # irreducible under the prediction model

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
