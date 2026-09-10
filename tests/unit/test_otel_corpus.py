"""Unit test for the OTel corpus batch driver (#79). Runs --dry-run (no cluster,
no network, no waiting) and checks it emits a harness-loadable corpus."""
import importlib.util
from pathlib import Path

from src.eval.case import load_cases
from src.eval.otel_demo import FLAG_SCENARIOS

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "eval" / "otel_corpus.py"


def _run_dry(out_dir: Path, negatives: int, monkeypatch):
    spec = importlib.util.spec_from_file_location("otel_corpus", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr("sys.argv", [
        "otel_corpus.py", "--dry-run", "--out-dir", str(out_dir), "--negatives", str(negatives),
    ])
    assert mod.main() == 0


def test_dry_run_emits_loadable_corpus(tmp_path, monkeypatch):
    _run_dry(tmp_path, negatives=3, monkeypatch=monkeypatch)
    cases = load_cases(tmp_path)
    # one case per flag scenario + the negatives
    assert len(cases) == len(FLAG_SCENARIOS) + 3

    positives = [c for c in cases if c.expect_explanation]
    negatives = [c for c in cases if not c.expect_explanation]
    assert len(positives) == len(FLAG_SCENARIOS)
    assert len(negatives) == 3

    # every flag scenario's ground-truth service + trigger type made it into a case
    by_service = {c.root_cause.service for c in positives}
    assert by_service == {s.service for s in FLAG_SCENARIOS}
    assert all(c.trigger is not None and c.trigger.timestamp is not None for c in positives)
    assert all(c.root_cause is None for c in negatives)


def test_requires_otlp_dir_without_dry_run(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("otel_corpus", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr("sys.argv", ["otel_corpus.py", "--out-dir", str(tmp_path)])
    assert mod.main() == 2  # no --otlp-dir and not --dry-run -> usage error
