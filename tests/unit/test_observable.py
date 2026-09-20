"""Phase A (#178) — the availability-aware observable model and its invariants.

Acceptance criteria are enforced here as tests (not docs): UNKNOWN is neutral to scoring,
uncollectable never becomes incident evidence, the three states never collapse, the signature is
the *hypothesis prediction* projected over usable ids, coordinate identity is a set (unique ids),
confidence cannot corrupt the score, states are open-ended, and contradictory axes are rejected.
"""

import math

import pytest
from src.core.rca.observable import (
    Availability,
    Observable,
    ObservableSet,
    State,
    evidence_score,
    hypothesis_signature,
    integration_gaps,
    observed,
    uncollectable,
    unknown,
    usable_ids,
    usable_observables,
)


class TestThreeStatesNeverCollapse:
    def test_distinct(self):
        u, k, a = uncollectable("f"), unknown("f"), observed("f", State.ABSENT)
        assert (u.collectable, u.availability, u.state) == (False, Availability.UNKNOWN, None)
        assert (k.collectable, k.availability, k.state) == (True, Availability.UNKNOWN, None)
        assert (a.collectable, a.availability, a.state) == (True, Availability.OBSERVED, State.ABSENT)
        assert u != k and k != a and u != a

    def test_observed_absent_is_not_unknown(self):
        assert observed("f", State.ABSENT).is_usable is True
        assert unknown("f").is_usable is False

    def test_round_trip_preserves_the_distinction(self):
        for o in (uncollectable("f"), unknown("f"),
                  observed("f", State.PRESENT, value=1.0, baseline=State.ABSENT)):
            assert Observable.from_dict(o.to_dict()) == o


class TestAxisAndValueContracts:
    def test_observed_requires_state(self):
        with pytest.raises(ValueError):
            Observable(id="f", availability=Availability.OBSERVED, state=None)

    def test_unknown_must_not_carry_state(self):
        with pytest.raises(ValueError):
            Observable(id="f", availability=Availability.UNKNOWN, state=State.PRESENT)

    def test_uncollectable_cannot_be_observed(self):
        # contradictory axes: never-producible AND measured
        with pytest.raises(ValueError):
            Observable(id="f", collectable=False, availability=Availability.OBSERVED,
                       state=State.PRESENT)

    @pytest.mark.parametrize("bad", [-1.0, 2.0, math.nan, math.inf, -math.inf])
    def test_confidence_must_be_finite_unit_interval(self, bad):
        with pytest.raises(ValueError):
            observed("f", State.PRESENT, measurement_confidence=bad)

    def test_states_are_open_ended(self):
        o = observed("f", "saturated")  # deployment-specific state, not a compiled-in constant
        assert o.state == "saturated"
        assert Observable.from_dict(o.to_dict()) == o  # round-trips


class TestRuntimeRepresentationIsValidated:
    """No static type gate: dynamic/deserialized input must fail at construction, not in a consumer."""

    def test_from_dict_rejects_non_string_state(self):
        with pytest.raises(ValueError):
            Observable.from_dict({"id": "f", "availability": "observed", "state": 7})

    def test_from_dict_rejects_non_string_baseline(self):
        with pytest.raises(ValueError):
            Observable.from_dict({"id": "f", "availability": "observed", "state": "present",
                                  "baseline": 3})

    def test_raw_string_availability_is_normalized_to_the_enum(self):
        o = Observable(id="f", availability="observed", state=State.PRESENT)  # type: ignore[arg-type]
        assert o.availability is Availability.OBSERVED
        assert o.to_dict()["availability"] == "observed"  # would crash if left a raw str

    def test_unknown_availability_string_is_rejected(self):
        with pytest.raises(ValueError):
            Observable(id="f", availability="maybe")  # type: ignore[arg-type]

    def test_empty_or_non_string_id_rejected(self):
        for bad in ("", 7, None):
            with pytest.raises(ValueError):
                observed(bad, State.PRESENT)  # type: ignore[arg-type]

    def test_bool_is_not_a_valid_confidence_or_value(self):
        with pytest.raises(ValueError):
            observed("f", State.PRESENT, measurement_confidence=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            observed("f", State.PRESENT, value=True)  # type: ignore[arg-type]


class TestConstructorsDoNotAlias:
    def test_unknown_is_always_collectable(self):
        # `unknown` must not be able to manufacture the uncollectable state via a flag.
        assert unknown("f").collectable is True
        with pytest.raises(TypeError):
            unknown("f", collectable=False)  # type: ignore[call-arg]

    def test_unknown_and_uncollectable_are_distinct_states(self):
        assert unknown("f") != uncollectable("f")


class TestCoordinateIsASet:
    def test_duplicate_ids_are_rejected(self):
        dup = [observed("a", State.PRESENT), observed("a", State.PRESENT)]
        for fn in (usable_observables, usable_ids, integration_gaps):
            with pytest.raises(ValueError):
                fn(dup)
        with pytest.raises(ValueError):
            evidence_score({"a": State.PRESENT}, dup)
        with pytest.raises(ValueError):
            ObservableSet(dup)

    def test_contradictory_same_id_rejected(self):
        with pytest.raises(ValueError):
            usable_observables([observed("a", State.PRESENT), observed("a", State.ABSENT)])


class TestHypothesisSignatureIsPredictionProjectedOverUsableIds:
    def test_signature_uses_prediction_not_observation(self):
        obs = [observed("a", State.PRESENT), observed("b", State.HIGH)]
        prediction = {"a": State.ABSENT, "b": State.HIGH}  # a predicted ABSENT though observed PRESENT
        # S_O(C) carries the PREDICTED states over usable ids, not the observed ones
        assert hypothesis_signature(prediction, obs) == (("a", State.ABSENT), ("b", State.HIGH))

    def test_signature_excludes_unusable_ids(self):
        obs = [observed("a", State.PRESENT), unknown("b"), uncollectable("c")]
        prediction = {"a": State.PRESENT, "b": State.PRESENT, "c": State.PRESENT}
        # only 'a' is in F_usable; b (UNKNOWN) and c (uncollectable) are excluded
        assert usable_ids(obs) == ("a",)
        assert hypothesis_signature(prediction, obs) == (("a", State.PRESENT),)

    def test_unpredicted_usable_ids_are_omitted(self):
        obs = [observed("a", State.PRESENT), observed("b", State.PRESENT)]
        assert hypothesis_signature({"a": State.PRESENT}, obs) == (("a", State.PRESENT),)


class TestInvariant1UnknownIsNeutral:
    def test_adding_unknown_does_not_change_score(self):
        exp = {"a": State.PRESENT, "b": State.ABSENT, "c": State.HIGH}
        base = [observed("a", State.PRESENT), observed("b", State.ABSENT)]
        s0 = evidence_score(exp, base)
        for extra in (unknown("c"), unknown("z")):
            assert evidence_score(exp, base + [extra]) == s0

    def test_adding_unknown_does_not_change_usable_ids_or_signature(self):
        base = [observed("a", State.PRESENT)]
        pred = {"a": State.PRESENT}
        assert usable_ids(base + [unknown("b")]) == usable_ids(base) == ("a",)
        assert hypothesis_signature(pred, base + [unknown("b")]) == hypothesis_signature(pred, base)


class TestInvariant2UncollectableNeverEvidence:
    def test_uncollectable_never_scores_or_signs(self):
        obs = [observed("a", State.PRESENT), uncollectable("gap")]
        assert evidence_score({"a": State.PRESENT, "gap": State.PRESENT}, obs) == 1.0
        assert "gap" not in usable_ids(obs)
        assert hypothesis_signature({"gap": State.PRESENT, "a": State.PRESENT}, obs) == (
            ("a", State.PRESENT),
        )

    def test_uncollectable_surfaces_as_integration_gap(self):
        obs = [observed("a", State.PRESENT), uncollectable("gap"), unknown("k")]
        assert integration_gaps(obs) == ["gap"]  # UNKNOWN (collectable) is NOT a gap


class TestObservableSet:
    def test_delegates_to_invariant_functions(self):
        s = ObservableSet([observed("a", State.PRESENT), unknown("b"), uncollectable("c")])
        assert s.usable_ids() == ("a",)
        assert s.signature({"a": State.PRESENT}) == (("a", State.PRESENT),)
        assert s.score({"a": State.PRESENT}) == 1.0
        assert s.integration_gaps() == ["c"]

    def test_confidence_weights_the_score_and_is_bounded(self):
        assert evidence_score({"a": State.PRESENT},
                              [observed("a", State.PRESENT, measurement_confidence=0.5)]) == 0.5

    def test_uniqueness_survives_caller_mutation(self):
        items = [observed("a", State.PRESENT)]
        s = ObservableSet(items)
        items.append(observed("a", State.ABSENT))  # would create a contradictory duplicate
        assert s.usable_ids() == ("a",)  # the set defended its own representation
        assert s.score({"a": State.PRESENT}) == 1.0

    def test_representation_is_immutable(self):
        s = ObservableSet([observed("a", State.PRESENT)])
        assert isinstance(s.observables, tuple)
        with pytest.raises((AttributeError, TypeError)):
            s.observables = []  # type: ignore[misc]  # frozen: field cannot be reassigned
        with pytest.raises(AttributeError):
            s.observables.append(observed("b", State.PRESENT))  # type: ignore[attr-defined]
