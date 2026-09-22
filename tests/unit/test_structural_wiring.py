"""Phase I (#187) part 2 — the opt-in structural view's serialization contract. Pure, no DB.

The structural packet appears in the API response ONLY when the ExplainResult carries a structural
result, and its presence never changes the rest of the response (it is an additional, experimental
view). The live adapter that produces the result from telemetry is covered by an integration test.
"""

from datetime import datetime, timezone

from src.api.schemas.v1 import explain_from_result
from src.core.explain.summarizer import ExplainResult
from src.core.rca.expectations import ExpectedObservation, ObservationModel, Strength
from src.core.rca.hypothesis import Kind, from_observation_model
from src.core.rca.observable import observed
from src.core.rca.outcome import resolve
from src.core.rca.partition import partition
from src.core.rca.scoring import rank_classes

_W0 = datetime(2026, 3, 12, 13, 0, 0, tzinfo=timezone.utc)
_W1 = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)


def _base_result(**extra) -> ExplainResult:
    return ExplainResult(
        window_start=_W0, window_end=_W1, summary_text="s", confidence="low",
        evidence_items=[], services_affected=["api"], total_logs=1, mode="rules", **extra,
    )


def _structural():
    h = from_observation_model(
        "process:payment", Kind.PROCESS, "payment",
        ObservationModel(expected=(ExpectedObservation("sig:payment", "present", Strength.USUALLY),)),
    )
    p = partition([h], [observed("sig:payment", "present")])
    return resolve(p), rank_classes(p)


def test_structural_absent_by_default():
    resp = explain_from_result(_base_result(), no_llm=True, cached=False)
    assert resp.structural is None


def test_structural_present_when_result_carries_it():
    result, ranking = _structural()
    resp = explain_from_result(
        _base_result(structural_result=result, structural_ranking=ranking),
        no_llm=True, cached=False,
    )
    assert resp.structural is not None
    assert resp.structural["outcome"] == "identified"
    assert resp.structural["localization"] == ["payment"]


def test_structural_does_not_alter_the_rest_of_the_response():
    plain = explain_from_result(_base_result(), no_llm=True, cached=False)
    result, ranking = _structural()
    withstruct = explain_from_result(
        _base_result(structural_result=result, structural_ranking=ranking),
        no_llm=True, cached=False,
    )
    a = plain.model_dump(by_alias=True)
    b = withstruct.model_dump(by_alias=True)
    a.pop("structural"), b.pop("structural")
    assert a == b  # every other field is identical
