"""Freeze the Phase F (#184) disappearance benchmark: the cause is reachable in the baseline and goes
silent (no spans) in the incident. The corpus is defined by the deterministic generator, so this test
pins the phenomenon's contract before Phase F is built (and guards the existing families). No DB."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from random import Random

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "gen_trace_localization_corpus.py"
_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _gen():
    spec = importlib.util.spec_from_file_location("gen_trace_localization_corpus", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _span_counts(spans, service):
    base = sum(1 for s in spans if s["service"] == service
               and datetime.fromisoformat(s["start_time"]) < _T0)
    inc = sum(1 for s in spans if s["service"] == service
              and datetime.fromisoformat(s["start_time"]) >= _T0)
    return base, inc


class TestDisappearanceContract:
    def test_cause_has_baseline_spans_and_no_incident_spans(self):
        gen = _gen()
        for topo, cause in (("shop", "payment"), ("orders", "ledger"), ("media", "storage")):
            data = gen._gen_case(topo, "callee_vanish", Random(1), _T0)
            assert data["cause"] == cause and data["labels"]["fault_type"] == "callee_vanish"
            base, inc = _span_counts(data["spans"], cause)
            assert base > 0 and inc == 0, f"{topo}: baseline={base} incident={inc}"

    def test_caller_errors_when_callee_is_unreachable(self):
        gen = _gen()
        data = gen._gen_case("shop", "callee_vanish", Random(1), _T0)
        symptom = data["labels"]["symptom_services"][0]  # checkout
        # the caller shows error status spans in the incident (it can't reach the vanished callee)
        err = [s for s in data["spans"] if s["service"] == symptom and s["status_code"] == "2"
               and datetime.fromisoformat(s["start_time"]) >= _T0]
        assert err, "the caller must error when its callee has vanished"

    def test_labels_are_frozen(self):
        gen = _gen()
        data = gen._gen_case("media", "callee_vanish", Random(1), _T0)
        lab = data["labels"]
        assert lab["root_cause"] == "storage" and lab["first_failing"] == "storage"
        assert lab["symptom_services"] == ["media"]


class TestExistingFamiliesUnaffected:
    def test_non_disappearance_cause_still_present_in_incident(self):
        # regression: the truncation must only apply to disappearance families
        gen = _gen()
        data = gen._gen_case("shop", "callee_fail", Random(1), _T0)
        _base, inc = _span_counts(data["spans"], "payment")
        assert inc > 0  # callee_fail's cause keeps emitting (erroring) spans in the incident

    def test_subtree_of_a_leaf_is_itself(self):
        gen = _gen()
        assert gen._subtree(gen.TOPOLOGIES["shop"], "payment") == {"payment"}
        assert gen._subtree(gen.TOPOLOGIES["media"], "media") == {"media", "storage", "transcoder"}
