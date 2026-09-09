"""Unit tests for surfacing the ranker's root-cause prediction in the API
response (#118 C2 follow-up). No DB."""
from datetime import datetime, timezone

import pytest

from src.api.schemas.v1 import explain_from_result, rca_candidates_from
from src.core.explain.summarizer import ExplainResult
from src.core.rca.candidates import build_candidates
from src.core.rca.features import FeatureTable, ServiceFeatures

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _result(**overrides) -> ExplainResult:
    base = dict(
        window_start=T0,
        window_end=T0,
        summary_text="ok",
        confidence="low",
        evidence_items=[],
        services_affected=[],
    )
    base.update(overrides)
    return ExplainResult(**base)


class TestRcaCandidatesFrom:
    def test_maps_candidate_to_dict_shape(self):
        table = FeatureTable(
            services=[ServiceFeatures(service="pay", tr_rate=2.0, met_anom=4.0)],
            has_traces=True, has_metrics=True,
        )
        raw = [c.to_dict() for c in build_candidates(table, scorer=lambda s: s.met_anom)]
        models = rca_candidates_from(raw)
        assert len(models) == 1
        assert models[0].service == "pay"
        assert models[0].score == 4.0
        assert set(models[0].modalities) == {"traces", "metrics"}
        assert all(e.detail for e in models[0].evidence)

    def test_tolerates_non_list(self):
        assert rca_candidates_from(None) == []
        assert rca_candidates_from("nonsense") == []


class TestExplainResponseSurface:
    def test_carries_prediction_and_candidates(self):
        result = _result(
            predicted_root_cause="paymentservice",
            root_cause_candidates=[
                {"service": "paymentservice", "score": 0.9, "modalities": ["metrics"],
                 "evidence": [{"modality": "metrics", "detail": "metric change 4.0x baseline"}]},
            ],
        )
        resp = explain_from_result(result, no_llm=True, cached=False)
        assert resp.predicted_root_cause == "paymentservice"
        assert resp.root_cause_candidates[0].service == "paymentservice"
        assert resp.root_cause_candidates[0].evidence[0].modality == "metrics"

    def test_empty_by_default(self):
        resp = explain_from_result(_result(), no_llm=True, cached=False)
        assert resp.predicted_root_cause is None
        assert resp.root_cause_candidates == []
        assert resp.predicted_root_cause_confidence is None

    def test_carries_calibrated_confidence(self):
        result = _result(predicted_root_cause="pay", predicted_root_cause_confidence=0.83)
        resp = explain_from_result(result, no_llm=True, cached=False)
        assert resp.predicted_root_cause_confidence == pytest.approx(0.83)
