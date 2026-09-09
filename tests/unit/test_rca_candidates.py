"""Unit tests for modality-neutral root-cause candidates (#118 C1c). No DB."""
from src.core.rca.candidates import (
    RootCauseCandidate,
    build_candidates,
    default_scorer,
)
from src.core.rca.features import FeatureTable, ServiceFeatures


def _sf(service, **kw):
    return ServiceFeatures(service=service, **kw)


class TestEvidenceAssembly:
    def test_evidence_only_for_firing_modalities(self):
        table = FeatureTable(
            services=[_sf("cart", log_err=3, log_grp=2, log_stack=1, met_anom=4.0)],
            has_logs=True, has_metrics=True,
        )
        cand = build_candidates(table)[0]
        assert cand.modalities == ["logs", "metrics"]  # no traces evidence
        logs = next(e for e in cand.evidence if e.modality == "logs")
        assert logs.signals == {"log_err": 3.0, "log_grp": 2.0, "log_stack": 1.0}
        assert "3 error lines" in logs.detail

    def test_trace_only_candidate_has_trace_evidence(self):
        table = FeatureTable(services=[_sf("pay", tr_rate=2.5, tr_dur=1.2)], has_traces=True)
        cand = build_candidates(table)[0]
        assert cand.modalities == ["traces"]
        assert cand.evidence[0].signals == {"tr_rate": 2.5, "tr_dur": 1.2}

    def test_service_with_no_signal_has_no_evidence(self):
        table = FeatureTable(services=[_sf("idle")])
        assert build_candidates(table)[0].evidence == []


class TestScoringAndOrder:
    def test_default_scorer_is_error_group(self):
        assert default_scorer(_sf("x", log_grp=7)) == 7.0

    def test_sorted_by_score_then_service(self):
        table = FeatureTable(
            services=[_sf("a", log_grp=1), _sf("b", log_grp=5), _sf("c", log_grp=5)],
        )
        ranked = build_candidates(table)
        assert [c.service for c in ranked] == ["b", "c", "a"]  # 5,5 (b<c), then 1

    def test_injectable_scorer_overrides(self):
        table = FeatureTable(services=[_sf("a", log_grp=1, met_anom=9.0), _sf("b", log_grp=5)])
        ranked = build_candidates(table, scorer=lambda sf: sf.met_anom)
        assert ranked[0].service == "a"  # met_anom 9 beats b's 0


class TestSerialization:
    def test_to_dict_shape(self):
        table = FeatureTable(services=[_sf("cart", log_err=1, log_grp=1)], has_logs=True)
        d = build_candidates(table)[0].to_dict()
        assert d["service"] == "cart"
        assert d["modalities"] == ["logs"]
        assert d["evidence"][0]["modality"] == "logs"
        assert "features" in d and d["features"]["service"] == "cart"

    def test_candidate_dataclass_fields(self):
        c = RootCauseCandidate(service="s", score=1.0, features=_sf("s"))
        assert c.evidence == [] and c.modalities == []
