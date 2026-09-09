"""Unit test for the RCA-ranker leave-one-*-out harness (#118 C2b). Verifies the
orchestration trains, serialises, and scores through the committed RcaRanker on a
small separable synthetic table — no HF download, no DB."""
import importlib.util
from pathlib import Path

from src.core.rca.features import FEATURE_NAMES

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "eval_rca_ranker.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("eval_rca_ranker", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(case, system, service, label, met_anom=0.0):
    r = {f: 0 for f in FEATURE_NAMES}
    r.update(case=case, system=system, svc=service, fault="f1", service=service,
             label=label, met_anom=met_anom)
    return r


def test_leave_one_system_out_learns_separable_signal():
    mod = _load_module()
    # Three systems; in every case the injected service has a high met_anom and
    # the decoy has none. A ranker trained on two systems should nail the third.
    rows = []
    for sysname in ("a", "b", "c"):
        for i in range(6):
            case = f"{sysname}{i}"
            rows.append(_row(case, sysname, f"{sysname}-inj{i}", 1, met_anom=5.0))
            rows.append(_row(case, sysname, f"{sysname}-decoy{i}", 0, met_anom=0.0))

    per, hits, total = mod._leave_one_out(rows, "system")
    assert total == 18  # 6 cases x 3 held-out systems
    assert hits == 18   # separable signal generalises to every held-out system
    assert set(per) == {"a", "b", "c"}


def test_load_derives_axes_from_case_name(tmp_path):
    import json

    mod = _load_module()
    p = tmp_path / "f.jsonl"
    row = {f: 0 for f in FEATURE_NAMES}
    row.update(case="re3tt_ts_order_service_code_2", system="tt", label=1)
    p.write_text(json.dumps(row) + "\n")
    loaded = mod._load(p)
    assert loaded[0]["svc"] == "ts_order_service"
    assert loaded[0]["fault"] == "code"
