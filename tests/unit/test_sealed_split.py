"""Unit tests for the sealed DEV/TEST split discipline (#118). No DB."""
import pytest

from src.eval.sealed_split import (
    SealedError,
    dev_ids,
    fault_family,
    load_manifest,
    make_split,
    test_ids as sealed_test_ids,
    write_manifest,
)


class TestFaultFamily:
    def test_precedence(self):
        assert fault_family({"fault_family": "x"}) == "x"
        assert fault_family({"trace_localization": {"fault_type": "symptom_only"}}) == "symptom_only"
        assert fault_family({"notes": "OTel Demo flag=paymentFailure (crit)"}) == "paymentFailure"
        assert fault_family({"trigger": {"type": "resource"}}) == "resource"
        assert fault_family({"expect_explanation": False}) == "negative"
        assert fault_family({}) == "unknown"

    def test_explicit_family_wins_over_flag(self):
        assert fault_family({"fault_family": "cpu", "notes": "flag=paymentFailure"}) == "cpu"


class TestMakeSplit:
    def _cases(self):
        return {
            "a": {"trace_localization": {"fault_type": "callee_fail"}, "root_cause": {"service": "payment"}},
            "b": {"trace_localization": {"fault_type": "symptom_only"}, "root_cause": {"service": "payment"}},
            "c": {"trace_localization": {"fault_type": "latency_only"}, "root_cause": {"service": "cart"}},
            "d": {"trace_localization": {"fault_type": "callee_fail"}, "root_cause": {"service": "cart"}},
        }

    def test_holdout_family_moves_whole_family(self):
        s = make_split(self._cases(), holdout_families=["symptom_only", "latency_only"])
        assert s.test == ["b", "c"] and s.dev == ["a", "d"]

    def test_holdout_service(self):
        s = make_split(self._cases(), holdout_services=["cart"])
        assert s.test == ["c", "d"] and s.dev == ["a", "b"]

    def test_requires_some_holdout(self):
        with pytest.raises(ValueError):
            make_split(self._cases())

    def test_deterministic_sorted(self):
        s1 = make_split(self._cases(), holdout_families=["callee_fail"])
        s2 = make_split(self._cases(), holdout_families=["callee_fail"])
        assert s1.test == s2.test == ["a", "d"]


class TestManifestSeal:
    def _write(self, tmp_path):
        cases = {
            "a": {"trace_localization": {"fault_type": "callee_fail"}},
            "b": {"trace_localization": {"fault_type": "symptom_only"}},
        }
        split = make_split(cases, holdout_families=["symptom_only"])
        return write_manifest(tmp_path, split), split

    def test_write_and_load_roundtrip(self, tmp_path):
        _, split = self._write(tmp_path)
        m = load_manifest(tmp_path)
        assert dev_ids(m) == ["a"]
        assert m["test_fingerprint"] == split.test_fingerprint

    def test_test_ids_sealed_by_default(self, tmp_path):
        self._write(tmp_path)
        m = load_manifest(tmp_path)
        with pytest.raises(SealedError):
            sealed_test_ids(m)
        assert sealed_test_ids(m, unseal=True) == ["b"]

    def test_refuses_to_clobber(self, tmp_path):
        _, split = self._write(tmp_path)
        with pytest.raises(SealedError):
            write_manifest(tmp_path, split)

    def test_fingerprint_mismatch_detected(self, tmp_path):
        self._write(tmp_path)
        p = tmp_path / "split.yaml"
        p.write_text(p.read_text().replace("- b", "- b\n- c"))  # tamper: add a test id
        with pytest.raises(SealedError):
            load_manifest(tmp_path)
