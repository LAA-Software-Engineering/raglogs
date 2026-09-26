"""Unit test for the OTel corpus batch driver (#79). Runs --dry-run (no cluster,
no network, no waiting) and checks it emits a harness-loadable corpus."""
import importlib.util
from pathlib import Path

from src.eval.case import load_cases
from src.eval.otel_demo import CHAOS_SCENARIOS, FLAG_SCENARIOS

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "eval" / "otel_corpus.py"


def _run(argv, monkeypatch):
    spec = importlib.util.spec_from_file_location("otel_corpus", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr("sys.argv", ["otel_corpus.py", *argv])
    return mod.main()


def _run_dry(out_dir: Path, negatives: int, monkeypatch):
    assert _run(["--dry-run", "--out-dir", str(out_dir), "--negatives", str(negatives)], monkeypatch) == 0


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
    assert _run(["--out-dir", str(tmp_path)], monkeypatch) == 2  # no --otlp-dir, not --dry-run


def test_chaos_dry_run_emits_loadable_corpus(tmp_path, monkeypatch):
    assert _run(["--chaos", "--dry-run", "--out-dir", str(tmp_path)], monkeypatch) == 0
    cases = load_cases(tmp_path)
    assert len(cases) == len(CHAOS_SCENARIOS)
    assert {c.root_cause.service for c in cases} == {s.service for s in CHAOS_SCENARIOS}
    assert {c.trigger.type for c in cases} == {s.trigger_type for s in CHAOS_SCENARIOS}


class _FakeKubectl:
    """Stands in for ``subprocess.run``: serves a synthetic kubeconfig to ``kubectl config view``
    and records every other call. No test here can reach a real kubectl or cluster."""

    KUBECONFIG = {
        "current-context": "cfbc-live-prod",
        "contexts": [
            {"name": "cfbc-live-prod", "context": {"cluster": "cfbc-live-prod"}},
            {"name": "kind-otel", "context": {"cluster": "kind-otel"}},
            # innocuous alias for a production cluster
            {"name": "sandbox", "context": {"cluster": "arn:aws:eks:us-east-1:1:cluster/cfbc-live-prod"}},
            {"name": "delivery-dev", "context": {"cluster": "delivery-dev"}},
        ],
        "clusters": [
            {"name": "cfbc-live-prod", "cluster": {"server": "https://ABC.gr7.us-east-1.eks.amazonaws.com"}},
            {"name": "kind-otel", "cluster": {"server": "https://127.0.0.1:6443"}},
            {"name": "arn:aws:eks:us-east-1:1:cluster/cfbc-live-prod",
             "cluster": {"server": "https://DEF.gr7.us-east-1.eks.amazonaws.com"}},
            {"name": "delivery-dev", "cluster": {"server": "https://10.0.0.5:6443"}},
        ],
    }

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kw):
        import json
        import subprocess

        if argv[:3] == ["kubectl", "config", "view"]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(self.KUBECONFIG), stderr="")
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _run_chaos(tmp_path, monkeypatch, *extra):
    fake = _FakeKubectl()
    monkeypatch.setattr("subprocess.run", fake)
    code = _run(["--chaos", "--otlp-dir", str(tmp_path / "otlp"), "--out-dir", str(tmp_path / "out"),
                 "--baseline", "0", "--post", "0", *extra], monkeypatch)
    return code, fake


class TestKubeContextGuard:
    def test_real_chaos_without_kube_context_refuses_and_runs_no_kubectl(self, tmp_path, monkeypatch):
        code, fake = _run_chaos(tmp_path, monkeypatch)
        assert code == 2 and fake.calls == []  # never falls back to the current (prod) context

    def test_prod_named_context_is_refused(self, tmp_path, monkeypatch):
        code, fake = _run_chaos(tmp_path, monkeypatch, "--kube-context", "cfbc-live-prod")
        assert code == 2 and fake.calls == []

    def test_alias_pointing_at_a_prod_cluster_is_refused(self, tmp_path, monkeypatch):
        code, fake = _run_chaos(tmp_path, monkeypatch, "--kube-context", "sandbox")
        assert code == 2 and fake.calls == []

    def test_unknown_context_is_refused(self, tmp_path, monkeypatch):
        code, fake = _run_chaos(tmp_path, monkeypatch, "--kube-context", "kind-typo")
        assert code == 2 and fake.calls == []

    def test_allow_context_must_repeat_the_exact_name(self, tmp_path, monkeypatch):
        code, fake = _run_chaos(tmp_path, monkeypatch, "--kube-context", "cfbc-live-prod",
                                "--allow-context", "delivery-dev")
        assert code == 2 and fake.calls == []

    def test_allow_context_does_not_admit_a_missing_context(self, tmp_path, monkeypatch):
        code, fake = _run_chaos(tmp_path, monkeypatch, "--kube-context", "kind-typo",
                                "--allow-context", "kind-typo")
        assert code == 2 and fake.calls == []

    def test_marker_false_positive_needs_allow_context(self, tmp_path, monkeypatch):
        code, fake = _run_chaos(tmp_path, monkeypatch, "--kube-context", "delivery-dev")  # "live" in "delivery"
        assert code == 2 and fake.calls == []
        code, fake = _run_chaos(tmp_path, monkeypatch, "--kube-context", "delivery-dev",
                                "--allow-context", "delivery-dev")
        assert code == 0 and fake.calls

    def test_every_kubectl_call_names_the_context(self, tmp_path, monkeypatch):
        code, fake = _run_chaos(tmp_path, monkeypatch, "--kube-context", "kind-otel")
        assert code == 0
        assert len(fake.calls) == 2 * len(CHAOS_SCENARIOS)  # one apply + one delete per scenario
        for argv in fake.calls:
            assert argv[:3] == ["kubectl", "--context", "kind-otel"], argv
        assert [a[3] for a in fake.calls] == ["apply", "delete"] * len(CHAOS_SCENARIOS)

    def test_dry_run_never_touches_kubectl(self, tmp_path, monkeypatch):
        fake = _FakeKubectl()
        monkeypatch.setattr("subprocess.run", fake)
        assert _run(["--chaos", "--dry-run", "--out-dir", str(tmp_path)], monkeypatch) == 0
        assert fake.calls == []


def test_every_chaos_scenario_has_a_committed_manifest():
    chaos_dir = _REPO / "deploy" / "otel-demo" / "chaos"
    for s in CHAOS_SCENARIOS:
        assert (chaos_dir / f"{s.name}.yaml").is_file(), f"missing manifest for {s.name}"
