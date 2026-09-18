"""Phase A (#178) — the availability-aware observable model and its invariants.

Acceptance criteria are enforced here as tests (not docs): UNKNOWN is neutral to scoring,
uncollectable never becomes incident evidence, and the three states never collapse.
"""

import pytest
from src.core.rca.observable import (
    Availability,
    Observable,
    ObservableSet,
    State,
    evidence_score,
    integration_gaps,
    observed,
    uncollectable,
    unknown,
    usable_observables,
    usable_signature,
)


class TestThreeStatesNeverCollapse:
    def test_distinct(self):
        u = uncollectable("f")          # not collectable here
        k = unknown("f")                # collectable, not measured this incident
        a = observed("f", State.ABSENT)  # measured, and absent
        # all three are different objects with different semantics
        assert (u.collectable, u.availability, u.state) == (False, Availability.UNKNOWN, None)
        assert (k.collectable, k.availability, k.state) == (True, Availability.UNKNOWN, None)
        assert (a.collectable, a.availability, a.state) == (True, Availability.OBSERVED, State.ABSENT)
        assert u != k and k != a and u != a

    def test_observed_absent_is_not_unknown(self):
        # the whole point: "measured absent" must not read as "not measured"
        assert observed("f", State.ABSENT).is_usable is True
        assert unknown("f").is_usable is False

    def test_round_trip_preserves_the_distinction(self):
        for o in (uncollectable("f"), unknown("f"), observed("f", State.PRESENT, value=1.0,
                                                              baseline=State.ABSENT)):
            assert Observable.from_dict(o.to_dict()) == o

    def test_illegal_states_are_rejected(self):
        with pytest.raises(ValueError):
            Observable(id="f", availability=Availability.OBSERVED, state=None)   # observed w/o state
        with pytest.raises(ValueError):
            Observable(id="f", availability=Availability.UNKNOWN, state=State.PRESENT)  # unknown w/ state


class TestInvariant1UnknownIsNeutral:
    """score(H, O ∪ {UNKNOWN(f)}) == score(H, O) — adding/removing UNKNOWN changes no score."""

    def test_adding_unknown_does_not_change_score(self):
        exp = {"a": State.PRESENT, "b": State.ABSENT, "c": State.HIGH}
        base = [observed("a", State.PRESENT), observed("b", State.ABSENT)]
        s0 = evidence_score(exp, base)
        for extra in (unknown("c"), unknown("z"), unknown("a")):  # incl. one the hypothesis expects
            assert evidence_score(exp, base + [extra]) == s0

    def test_unknown_not_in_usable_or_signature(self):
        obs = [observed("a", State.PRESENT), unknown("b")]
        assert [o.id for o in usable_observables(obs)] == ["a"]
        assert usable_signature(obs) == (("a", "present"),)


class TestInvariant2UncollectableNeverEvidence:
    def test_uncollectable_never_scores_or_signs(self):
        exp = {"a": State.PRESENT, "gap": State.PRESENT}
        obs = [observed("a", State.PRESENT), uncollectable("gap")]
        # scores exactly as if the uncollectable one were not there
        assert evidence_score(exp, obs) == evidence_score(exp, [observed("a", State.PRESENT)])
        assert ("gap", "present") not in usable_signature(obs)
        assert all(o.id != "gap" for o in usable_observables(obs))

    def test_uncollectable_surfaces_as_integration_gap(self):
        obs = [observed("a", State.PRESENT), uncollectable("gap"), unknown("k")]
        # uncollectable -> integration gap; UNKNOWN (collectable) is NOT an integration gap
        assert integration_gaps(obs) == ["gap"]


class TestObservableSet:
    def test_container_delegates_to_invariant_functions(self):
        s = ObservableSet([observed("a", State.PRESENT), unknown("b"), uncollectable("c")])
        assert [o.id for o in s.usable()] == ["a"]
        assert s.signature() == (("a", "present"),)
        assert s.score({"a": State.PRESENT}) == 1.0
        assert s.integration_gaps() == ["c"]

    def test_measurement_confidence_weights_the_score(self):
        obs = [observed("a", State.PRESENT, measurement_confidence=0.5)]
        assert evidence_score({"a": State.PRESENT}, obs) == 0.5
