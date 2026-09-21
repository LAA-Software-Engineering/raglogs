"""Phase E (#183) — the structural outcome, a pure total function of the partition.

Covers each of the five outcomes from constructed partitions (incl. an UNCERTAIN partition that
contains an internally-irreducible class), that the outcome ignores scores, that
NO_COMPATIBLE_HYPOTHESIS never claims a localization, and that hard-rule discriminators are surfaced
as potential_elimination_checks separately from D_missing.
"""

import pytest
from src.core.rca.candidates import RootCauseCandidate
from src.core.rca.expectations import Contradiction, ExpectedObservation, ObservationModel
from src.core.rca.features import ServiceFeatures
from src.core.rca.hypothesis import Hypothesis, Kind, from_observation_model
from src.core.rca.observable import observed, uncollectable
from src.core.rca.outcome import EvidenceItem, Outcome, Relation, classify, resolve
from src.core.rca.partition import partition


def _h(hid: str, predictions: dict, *, kind: Kind = Kind.PROCESS, score: float = 0.0,
       model=None) -> Hypothesis:
    cand = RootCauseCandidate(service=hid, score=score, features=ServiceFeatures(service=hid))
    if model is not None:
        return from_observation_model(hid, kind, hid, model, source=cand)
    return Hypothesis(id=hid, kind=kind, localization=hid, predictions=predictions, _source=cand)


class TestFiveOutcomes:
    def test_identified(self):
        p = partition([_h("h1", {"a": "present"})], [observed("a", "present")])
        assert classify(p) is Outcome.IDENTIFIED

    def test_non_identifiable_when_d_missing_nonempty(self):
        # equal usable signature, differ on the unobserved 'hidden' -> one class, D_missing={hidden}
        h1 = _h("h1", {"a": "present", "hidden": "present"})
        h2 = _h("h2", {"a": "present", "hidden": "absent"})
        p = partition([h1, h2], [observed("a", "present")])
        assert len(p.classes) == 1 and p.classes[0].d_missing == frozenset({"hidden"})
        assert classify(p) is Outcome.NON_IDENTIFIABLE

    def test_irreducible_when_d_missing_empty(self):
        # identical predictions everywhere -> one multi-member class, empty D_missing
        h1 = _h("h1", {"a": "present"})
        h2 = _h("h2", {"a": "present"}, kind=Kind.EDGE)
        p = partition([h1, h2], [observed("a", "present")])
        assert len(p.classes) == 1 and p.classes[0].d_missing == frozenset()
        assert classify(p) is Outcome.IRREDUCIBLE

    def test_uncertain_when_multiple_classes(self):
        p = partition([_h("h1", {"a": "present"}), _h("h2", {"a": "absent"})],
                      [observed("a", "present")])
        assert len(p.classes) == 2
        assert classify(p) is Outcome.UNCERTAIN

    def test_no_compatible_hypothesis_when_all_eliminated(self):
        m = ObservationModel(contradictions=(Contradiction("g", frozenset({"true"})),),
                             expected=(ExpectedObservation("a", "present"),))
        h = _h("h1", {}, model=m)
        p = partition([h], [observed("g", "true")])
        assert p.no_surviving_hypothesis
        assert classify(p) is Outcome.NO_COMPATIBLE_HYPOTHESIS


class TestTotalityAndPurity:
    def test_outcome_ignores_scores(self):
        a = partition([_h("h1", {"a": "present"}, score=0.01)], [observed("a", "present")])
        b = partition([_h("h1", {"a": "present"}, score=999.0)], [observed("a", "present")])
        assert classify(a) is classify(b) is Outcome.IDENTIFIED

    def test_uncertain_partition_may_contain_an_irreducible_class(self):
        # Class A = {h1, h2} identical predictions (irreducible); Class B = {h3} distinct
        h1 = _h("h1", {"a": "present"})
        h2 = _h("h2", {"a": "present"}, kind=Kind.EDGE)
        h3 = _h("h3", {"a": "absent"})
        p = partition([h1, h2, h3], [observed("a", "present")])
        assert classify(p) is Outcome.UNCERTAIN  # top-level stays UNCERTAIN
        res = resolve(p)
        assert any(v.is_irreducible for v in res.classes)  # ...though a class is internally irreducible


class TestPacket:
    def test_identified_packet(self):
        p = partition([_h("proc", {"a": "present"})], [observed("a", "present")])
        res = resolve(p, observations=[observed("a", "present")])
        assert res.outcome is Outcome.IDENTIFIED
        assert res.localization == ("proc",)
        assert res.classes[0].is_singleton and res.d_missing == frozenset()

    def test_non_identifiable_packet_reports_d_missing(self):
        h1 = _h("h1", {"a": "present", "hidden": "present"})
        h2 = _h("h2", {"a": "present", "hidden": "absent"})
        res = resolve(partition([h1, h2], [observed("a", "present")]))
        assert res.outcome is Outcome.NON_IDENTIFIABLE
        assert res.d_missing == frozenset({"hidden"})

    def test_no_compatible_hypothesis_claims_no_localization(self):
        m = ObservationModel(contradictions=(Contradiction("g", frozenset({"true"})),))
        res = resolve(partition([_h("h1", {}, model=m)], [observed("g", "true")]),
                      observations=[observed("g", "true")])
        assert res.outcome is Outcome.NO_COMPATIBLE_HYPOTHESIS
        assert res.localization == ()
        assert [e.member.id for e in res.eliminated] == ["h1"]


class TestSupportingEvidenceSurvives:
    def test_uncertain_split_carries_the_observed_fact_per_class(self):
        obs = [observed("a", "present")]
        h1 = _h("h1", {"a": "present"})
        h2 = _h("h2", {"a": "absent"})
        res = resolve(partition([h1, h2], obs), observations=obs)
        assert res.outcome is Outcome.UNCERTAIN
        # each class preserves its defining signature and cites 'a' with support vs mismatch
        by_sig = {v.signature: v for v in res.classes}
        supp = by_sig[(("a", "present"),)]
        miss = by_sig[(("a", "absent"),)]
        assert supp.evidence == (EvidenceItem("a", "present", "present", Relation.SUPPORTS),)
        assert miss.evidence == (EvidenceItem("a", "present", "absent", Relation.MISMATCH),)

    def test_all_eliminated_cites_the_eliminating_observation(self):
        m = ObservationModel(contradictions=(Contradiction("g", frozenset({"true"})),))
        obs = [observed("g", "true")]
        res = resolve(partition([_h("h1", {}, model=m)], obs), observations=obs)
        assert res.outcome is Outcome.NO_COMPATIBLE_HYPOTHESIS
        (ev,) = res.eliminated
        assert ev.member.id == "h1"
        assert ev.evidence == (EvidenceItem("g", "true", None, Relation.HARD_ELIMINATES),)

    def test_shared_localization_keeps_members_distinct(self):
        # two different ids/kinds sharing one localization must not collapse (Phase B's whole point)
        obs = [observed("a", "present")]
        h1 = Hypothesis(id="process:payment", kind=Kind.PROCESS, localization="payment",
                        predictions={"a": "present"})
        h2 = Hypothesis(id="edge:web-payment", kind=Kind.EDGE, localization="payment",
                        predictions={"a": "present"})
        res = resolve(partition([h1, h2], obs), observations=obs)
        (cls,) = res.classes
        assert [(m.id, m.kind, m.localization) for m in cls.members] == [
            ("edge:web-payment", "edge", "payment"),
            ("process:payment", "process", "payment"),
        ]
        assert res.localization == ("payment",)  # only the top-level summary dedupes

    def test_potential_elimination_checks_surface_hard_rules_not_in_d_missing(self):
        # h1 hard-contradicts g=true; g is UNKNOWN -> not usable. Same predictions -> IRREDUCIBLE.
        m1 = ObservationModel(expected=(ExpectedObservation("a", "present"),),
                              contradictions=(Contradiction("g", frozenset({"true"})),))
        m2 = ObservationModel(expected=(ExpectedObservation("a", "present"),))
        h1 = _h("h1", {}, model=m1)
        h2 = _h("h2", {}, model=m2)
        res = resolve(partition([h1, h2], [observed("a", "present")]))
        assert res.outcome is Outcome.IRREDUCIBLE
        assert res.d_missing == frozenset()                 # g is not a prediction distinguisher
        checks = res.potential_elimination_checks
        assert len(checks) == 1
        assert (checks[0].hypothesis_id, checks[0].observable_id) == ("h1", "g")
        assert checks[0].eliminating_states == ("true",)

    def test_integration_gaps_from_uncollectable_observations(self):
        p = partition([_h("h1", {"a": "present"})], [observed("a", "present")])
        res = resolve(p, observations=[observed("a", "present"), uncollectable("gap")])
        assert res.integration_gaps == ("gap",)


class TestPacketOwnsContainers:
    def test_no_compatible_hypothesis_with_localization_is_rejected(self):
        from src.core.rca.outcome import StructuralResult
        with pytest.raises(ValueError):
            StructuralResult(outcome=Outcome.NO_COMPATIBLE_HYPOTHESIS, localization=("x",),
                             classes=(), d_missing=frozenset(), potential_elimination_checks=())

    def test_result_snapshots_its_containers(self):
        from src.core.rca.outcome import StructuralResult
        loc = ["a"]
        res = StructuralResult(outcome=Outcome.IDENTIFIED, localization=loc, classes=(),
                               d_missing=frozenset(), potential_elimination_checks=())
        loc.clear()
        assert res.localization == ("a",)
        assert isinstance(res.localization, tuple)

    def test_empty_class_view_rejected(self):
        from src.core.rca.outcome import ClassView
        with pytest.raises(ValueError):
            ClassView(members=(), signature=(), d_missing=frozenset(), evidence=(),
                      elimination_checks=())
