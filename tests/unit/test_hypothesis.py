"""Phase B (#179) — the causal hypothesis ontology.

Acceptance criteria as tests: the six kinds are representable and kind/localization are distinct; a
service candidate wraps into a `process` hypothesis with order/score preserved (no ranker change);
the representation is validated and owned; and the Phase C stubs are inert.
"""

import pytest
from src.core.rca.candidates import (
    ModalityEvidence,
    RootCauseCandidate,
    build_candidates,
    default_scorer,
)
from src.core.rca.features import FeatureTable, ServiceFeatures
from src.core.rca.hypothesis import (
    Hypothesis,
    Kind,
    hypotheses_from_candidates,
    process_hypothesis_from_candidate,
)
from src.core.rca.observable import observed


def _sf(service: str, log_grp: int) -> ServiceFeatures:
    return ServiceFeatures(service=service, log_err=log_grp, log_grp=log_grp)


class TestOntology:
    def test_all_six_kinds_are_representable(self):
        kinds = {k.value for k in Kind}
        assert kinds == {"process", "edge", "resource", "infra_event", "change", "external"}
        for k in Kind:
            h = Hypothesis(id=f"{k.value}:x", kind=k, localization="x")
            assert h.kind is k

    def test_kind_and_localization_are_distinct(self):
        h = Hypothesis(id="edge:a->b", kind=Kind.EDGE, localization="a->b")
        assert h.kind is Kind.EDGE
        assert h.localization == "a->b"
        assert h.render() == "a->b"  # user-facing projection is localization only
        assert h.to_dict()["kind"] == "edge"  # structure retained internally

    def test_kind_string_is_coerced_to_the_enum(self):
        h = Hypothesis(id="p:s", kind="process", localization="s")  # type: ignore[arg-type]
        assert h.kind is Kind.PROCESS

    def test_unknown_kind_is_rejected(self):
        with pytest.raises(ValueError):
            Hypothesis(id="x", kind="database", localization="s")  # type: ignore[arg-type]

    def test_empty_id_or_localization_rejected(self):
        with pytest.raises(ValueError):
            Hypothesis(id="", kind=Kind.PROCESS, localization="s")
        with pytest.raises(ValueError):
            Hypothesis(id="p:s", kind=Kind.PROCESS, localization="")


class TestServiceToProcessMapping:
    def test_candidate_wraps_as_process_hypothesis(self):
        c = RootCauseCandidate(service="api", score=12.0, features=_sf("api", 12))
        h = process_hypothesis_from_candidate(c)
        assert h.kind is Kind.PROCESS
        assert h.localization == "api"
        assert h.id == "process:api"
        assert h.source.score == 12.0  # score/features reachable via provenance

    def test_mapping_preserves_ranker_order_and_scores(self):
        table = FeatureTable(services=[_sf("api", 3), _sf("db", 30), _sf("cache", 10)])
        candidates = build_candidates(table, scorer=default_scorer)
        hyps = hypotheses_from_candidates(candidates)
        # order-neutral: the hypotheses mirror the ranked candidates exactly
        assert [h.localization for h in hyps] == [c.service for c in candidates]
        assert [h.source.score for h in hyps] == [c.score for c in candidates]
        assert all(h.kind is Kind.PROCESS for h in hyps)
        # and the ranking itself is unchanged (db has the largest error group)
        assert [h.localization for h in hyps] == ["db", "cache", "api"]

    def test_empty_candidate_list_maps_to_empty(self):
        assert hypotheses_from_candidates([]) == []


class TestIdentityIsInvariantUnderRanking:
    """#177/#182: structural identity must not depend on the mutable ranker score."""

    def test_same_service_different_score_is_the_same_hypothesis(self):
        h1 = process_hypothesis_from_candidate(RootCauseCandidate("api", 1.0, _sf("api", 1)))
        h2 = process_hypothesis_from_candidate(RootCauseCandidate("api", 2.0, _sf("api", 9)))
        assert h1.id == h2.id
        assert h1 == h2  # identity is the causal object, not the score
        assert hash(h1) == hash(h2)
        assert {h1, h2} == {h1}  # dedupes as one causal hypothesis

    def test_mutating_the_source_candidate_cannot_rewrite_the_hypothesis(self):
        c = RootCauseCandidate("api", 1.0, _sf("api", 1))
        h = process_hypothesis_from_candidate(c)
        before = h.to_dict()
        c.score = 999.0  # mutate the caller's candidate after wrapping
        c.service = "db"
        assert h.source is not c  # owned snapshot, not an alias
        assert h.source.score == 1.0  # provenance frozen at wrap time
        assert h.localization == "api" and h.to_dict() == before  # no contradiction leaks in

    def test_mutating_source_property_result_cannot_rewrite_the_hypothesis(self):
        c = RootCauseCandidate("api", 1.0, _sf("api", 1),
                               evidence=[ModalityEvidence("logs", "d", {"log_err": 1.0})])
        h = process_hypothesis_from_candidate(c)
        before = h.to_dict()
        s = h.source           # copy-on-read: a throwaway snapshot
        s.service = "db"       # mutate it every way we can reach
        s.score = 999.0
        s.evidence[0].signals["log_err"] = 999.0
        assert h.source.score == 1.0 and h.source.service == "api"
        assert h.to_dict() == before  # internal provenance untouched

    def test_mutating_to_dict_output_cannot_rewrite_the_hypothesis(self):
        c = RootCauseCandidate("api", 1.0, _sf("api", 1),
                               evidence=[ModalityEvidence("logs", "d", {"log_err": 1.0})])
        h = process_hypothesis_from_candidate(c)
        before = h.to_dict()
        d = h.to_dict()
        d["source"]["score"] = 999.0
        d["source"]["evidence"][0]["signals"]["log_err"] = 999.0  # the flagged nested-dict leak
        assert h.to_dict() == before

    def test_source_must_be_a_candidate(self):
        with pytest.raises(ValueError):
            Hypothesis(id="p:s", kind=Kind.PROCESS, localization="s", _source="not-a-candidate")  # type: ignore[arg-type]

    def test_different_causal_fields_are_distinct(self):
        base = Hypothesis(id="process:api", kind=Kind.PROCESS, localization="api")
        assert base != Hypothesis(id="edge:api", kind=Kind.EDGE, localization="api")
        assert base != Hypothesis(id="process:api", kind=Kind.PROCESS, localization="api",
                                  predictions={"f": "present"})


class TestPhaseCStubsAreInert:
    def test_predictions_default_empty_and_hard_incompatibility_false(self):
        h = Hypothesis(id="p:s", kind=Kind.PROCESS, localization="s")
        assert dict(h.predictions) == {}
        assert h.hard_incompatibility([observed("f", "present")]) is False

    def test_predictions_are_owned_and_immutable(self):
        h = Hypothesis(id="p:s", kind=Kind.PROCESS, localization="s", predictions={"f": "present"})
        assert dict(h.predictions) == {"f": "present"}
        with pytest.raises(TypeError):
            h.predictions["g"] = "absent"  # type: ignore[index]

    def test_predictions_defensively_copied_from_caller(self):
        src = {"f": "present"}
        h = Hypothesis(id="p:s", kind=Kind.PROCESS, localization="s", predictions=src)
        src["g"] = "absent"  # mutating the caller's dict must not leak in
        assert dict(h.predictions) == {"f": "present"}

    def test_malformed_predictions_rejected(self):
        with pytest.raises(ValueError):
            Hypothesis(id="p:s", kind=Kind.PROCESS, localization="s", predictions={"f": 7})  # type: ignore[dict-item]
