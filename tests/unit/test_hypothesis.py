"""Phase B (#179) — the causal hypothesis ontology.

Acceptance criteria as tests: the six kinds are representable and kind/localization are distinct; a
service candidate wraps into a `process` hypothesis with order/score preserved (no ranker change);
the representation is validated and owned; and the Phase C stubs are inert.
"""

import pytest
from src.core.rca.candidates import RootCauseCandidate, build_candidates, default_scorer
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
        c = RootCauseCandidate(service="checkout", score=12.0, features=_sf("checkout", 12))
        h = process_hypothesis_from_candidate(c)
        assert h.kind is Kind.PROCESS
        assert h.localization == "checkout"
        assert h.id == "process:checkout"
        assert h.source is c  # provenance retained → score/features stay reachable
        assert h.source.score == 12.0

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
