"""Freeze the Phase F (#184) disappearance benchmark. The corpus is defined by the deterministic
generator (``data/`` is gitignored), so these tests pin the **actual published** six cases —
``generate_disappearance(seed=0)`` — by content hash and by invariant, before Phase F is built, so the
"before" corpus cannot drift with green tests. No DB."""

import hashlib
import importlib.util
from datetime import datetime
from pathlib import Path
from random import Random

import pytest
import yaml

from src.eval.trace_localization import load_trace_loc_labels

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "gen_trace_localization_corpus.py"
_CAUSE = {"shop": "payment", "orders": "ledger", "media": "storage"}

# Frozen content hashes of the published disappearance corpus (generate_disappearance, seed=0).
# Regenerate with: python scripts/gen_trace_localization_corpus.py --disappearance-out <dir>
_MANIFEST = {
    "tld_media_callee_vanish_0": "b5952977eae9d7defb016398f30863f3307a384d4e86d8efa982720d35169ed0",
    "tld_media_callee_vanish_1": "fb78a746a65bfa23c5049c51d70f2d6eb3d7cecba3833ef7352d08dede7aed4f",
    "tld_orders_callee_vanish_0": "a449d8dce49fdd3f236dd33b51fd5d042b4fd34d93309f4f3ac8e01ca8f6401c",
    "tld_orders_callee_vanish_1": "cb8e43bf35e3f1a74954ad0551c2c38f4b53d9fdd8f05ab8a874794da555f9e6",
    "tld_shop_callee_vanish_0": "b63114cf0c998608a48d4c90e577a2f628a4319ed5e9757822535c5304906b8f",
    "tld_shop_callee_vanish_1": "3426fa5ccd29bc6299c033f5a10173302888b85293255400116e6bb898bd7c0f",
}


def _gen():
    spec = importlib.util.spec_from_file_location("gen_trace_localization_corpus", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hash_case(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(d.iterdir()):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("tld")
    _gen().generate_disappearance(out, seed=0)
    return out


def _rows(path: Path):
    import json
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _topo(case_id: str) -> str:
    return case_id.split("_")[1]


class TestFrozenManifest:
    def test_published_cases_match_frozen_hashes(self, corpus):
        got = {d.name: _hash_case(d) for d in sorted(corpus.iterdir())}
        assert got == _MANIFEST  # any change to the benchmark must consciously update this manifest


class TestDisappearanceContract:
    def test_cause_has_baseline_spans_and_none_in_incident(self, corpus):
        for d in sorted(corpus.iterdir()):
            cause = _CAUSE[_topo(d.name)]
            w = yaml.safe_load((d / "case.yaml").read_text())
            ws = datetime.fromisoformat(w["window"]["start"])
            spans = _rows(d / "spans.jsonl")
            base = sum(1 for s in spans if s["service"] == cause
                       and datetime.fromisoformat(s["start_time"]) < ws)
            inc = sum(1 for s in spans if s["service"] == cause
                      and datetime.fromisoformat(s["start_time"]) >= ws)
            assert base > 0 and inc == 0, f"{d.name}: baseline={base} incident={inc}"

    def test_cause_metrics_stay_flat(self, corpus):
        # the whole adversarial point: the vanished cause emits NO error signal — only absence betrays
        # it. If this invariant broke, the documented 6/6 pre-F coverage baseline would be invalid.
        for d in sorted(corpus.iterdir()):
            cause = _CAUSE[_topo(d.name)]
            ws = datetime.fromisoformat(yaml.safe_load((d / "case.yaml").read_text())["window"]["start"])
            inc_err = [r["value"] for r in _rows(d / "metrics.jsonl")
                       if r["service"] == cause and r["metric"] == "error_rate"
                       and datetime.fromisoformat(r["ts"]) >= ws]
            assert inc_err and all(v == 0.01 for v in inc_err), f"{d.name}: {inc_err}"

    def test_caller_errors_when_callee_vanishes(self, corpus):
        for d in sorted(corpus.iterdir()):
            w = yaml.safe_load((d / "case.yaml").read_text())
            symptom = w["trace_localization"]["symptom_services"][0]
            ws = datetime.fromisoformat(w["window"]["start"])
            err = [s for s in _rows(d / "spans.jsonl")
                   if s["service"] == symptom and s["status_code"] == "2"
                   and datetime.fromisoformat(s["start_time"]) >= ws]
            assert err, f"{d.name}: caller {symptom} must error when its callee vanished"

    def test_labels_load_through_the_scorer(self, corpus):
        # regression for the vocabulary rejection: the label loader must accept callee_vanish
        for d in sorted(corpus.iterdir()):
            tc = load_trace_loc_labels(d / "case.yaml")
            assert tc is not None and tc.fault_type == "callee_vanish"
            assert tc.root_cause == _CAUSE[_topo(d.name)]


class TestExistingFamiliesUnaffected:
    def test_non_disappearance_cause_still_present_in_incident(self):
        gen = _gen()
        data = gen._gen_case("shop", "callee_fail", Random(1), datetime(2026, 1, 1, 12, 0, 0))
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        inc = sum(1 for s in data["spans"] if s["service"] == "payment"
                  and datetime.fromisoformat(s["start_time"]) >= t0)
        assert inc > 0  # callee_fail's cause keeps emitting (erroring) spans in the incident

    def test_subtree(self):
        gen = _gen()
        assert gen._subtree(gen.TOPOLOGIES["shop"], "payment") == {"payment"}
        assert gen._subtree(gen.TOPOLOGIES["media"], "media") == {"media", "storage", "transcoder"}
