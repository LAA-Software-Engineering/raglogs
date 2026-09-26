"""#209 M3 driver: the default-path check runs only with a ranker and calibrator that actually load.
The product loaders fail open (None -> volume selector / ordinal confidence); on a one-shot run that
would be a false result, so the driver refuses — before any DB work."""
import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "eval" / "m3_structural.py"
_RANKER = _REPO / "models" / "rca_ranker.json"
_CALIBRATOR = _REPO / "models" / "rca_calibrator.json"


def _mod():
    spec = importlib.util.spec_from_file_location("m3_structural", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_artifacts_load_with_provenance():
    out = _mod().load_artifacts(str(_RANKER), str(_CALIBRATOR))
    assert isinstance(out, dict)
    assert out["ranker_loaded"] and out["calibrator_loaded"]
    assert Path(out["ranker"]).is_absolute() and len(out["ranker_sha256"]) == 64


def test_missing_ranker_is_refused(tmp_path):
    reason = _mod().load_artifacts(str(tmp_path / "nope.json"), str(_CALIBRATOR))
    assert isinstance(reason, str) and "ranker" in reason


def test_corrupt_calibrator_is_refused(tmp_path):
    bad = tmp_path / "cal.json"
    bad.write_text("{not json")
    reason = _mod().load_artifacts(str(_RANKER), str(bad))
    assert isinstance(reason, str) and "calibrator" in reason


def test_main_refuses_before_touching_the_db(tmp_path, monkeypatch, capsys):
    mod = _mod()
    monkeypatch.setattr("sys.argv", ["m3_structural.py", str(tmp_path), "--json", str(tmp_path / "o.json"),
                                     "--md", str(tmp_path / "o.md"), "--allow-dirty",
                                     "--ranker", str(tmp_path / "missing.json")])
    monkeypatch.setattr("src.db.session.get_db", lambda: (_ for _ in ()).throw(AssertionError("DB touched")))
    assert mod.main() == 2
    assert "ranker" in capsys.readouterr().err
    assert not (tmp_path / "o.md").exists()
