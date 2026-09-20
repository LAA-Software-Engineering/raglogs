"""Phase C (#180) — observation expectations: hard incompatibility vs soft evidence.

Enforced as tests: hard contradictions eliminate while soft mismatches only move score (Invariant 3);
strength never changes the structural signature (Invariant 4); a missing symptom weakens but never
eliminates a process-death hypothesis (the observation-model regression).
"""

import pytest
from src.core.rca.expectations import (
    Contradiction,
    ExpectedObservation,
    ObservationModel,
    Strength,
    edge_network_failure_model,
    process_dead_model,
)
from src.core.rca.hypothesis import Kind, from_observation_model
from src.core.rca.observable import (
    hypothesis_signature,
    observed,
    uncollectable,
    unknown,
)


class TestValueContracts:
    def test_expected_observation_validates(self):
        with pytest.raises(ValueError):
            ExpectedObservation("", "present")
        with pytest.raises(ValueError):
            ExpectedObservation("f", "")
        with pytest.raises(ValueError):
            ExpectedObservation("f", "present", strength="always")  # type: ignore[arg-type]

    def test_strength_string_is_coerced(self):
        assert ExpectedObservation("f", "present", strength="usually").strength is Strength.USUALLY

    def test_contradiction_validates(self):
        with pytest.raises(ValueError):
            Contradiction("f", frozenset())  # empty
        with pytest.raises(ValueError):
            Contradiction("", frozenset({"healthy"}))

    def test_model_rejects_duplicate_coordinates(self):
        with pytest.raises(ValueError):
            ObservationModel(expected=(ExpectedObservation("f", "present"),
                                       ExpectedObservation("f", "absent")))
        with pytest.raises(ValueError):
            ObservationModel(contradictions=(Contradiction("f", frozenset({"a"})),
                                             Contradiction("f", frozenset({"b"}))))


class TestInvariant3HardSoftSeparation:
    def test_hard_contradiction_eliminates(self):
        m = process_dead_model("payment")
        # the process is positively confirmed serving — logically incompatible with "process dead"
        assert m.hard_incompatibility([observed("payment.health", "serving")]) is True

    def test_soft_mismatch_only_changes_score_never_eliminates(self):
        m = process_dead_model("payment")
        match = [observed("payment.error_log", "present")]
        mismatch = [observed("payment.error_log", "absent")]
        assert m.soft_support(match) > 0.0
        assert m.soft_support(mismatch) < 0.0        # a soft mismatch moves score down
        assert m.hard_incompatibility(mismatch) is False  # ...but never eliminates

    def test_unknown_and_uncollectable_neither_contradict_nor_score(self):
        m = process_dead_model("payment")
        obs = [unknown("payment.health"), uncollectable("payment.error_log")]
        assert m.hard_incompatibility(obs) is False
        assert m.soft_support(obs) == 0.0


class TestInvariant4StrengthDoesNotAlterIdentity:
    def test_predictions_are_strength_free(self):
        usually = ObservationModel(expected=(ExpectedObservation("f", "absent", Strength.USUALLY),))
        maybe = ObservationModel(expected=(ExpectedObservation("f", "absent", Strength.MAYBE),))
        assert usually.predictions() == maybe.predictions() == {"f": "absent"}

    def test_signature_and_equivalence_class_are_strength_invariant(self):
        obs = [observed("f", "absent")]
        h1 = from_observation_model("process:p", Kind.PROCESS, "p",
                                    ObservationModel(expected=(ExpectedObservation("f", "absent", Strength.USUALLY),)))
        h2 = from_observation_model("process:p", Kind.PROCESS, "p",
                                    ObservationModel(expected=(ExpectedObservation("f", "absent", Strength.MAYBE),)))
        # same categorical signature -> same equivalence class, despite different strength
        assert hypothesis_signature(h1.predictions, obs) == hypothesis_signature(h2.predictions, obs)
        assert h1 == h2 and hash(h1) == hash(h2)


class TestProcessDeathRegression:
    def test_missing_oom_and_restart_weaken_but_never_eliminate(self):
        m = process_dead_model("payment")
        present = [observed("payment.error_log", "present"),
                   observed("payment.restart", "present"),
                   observed("payment.oom", "present")]
        # the symptoms were collected and were ABSENT — the classic false-elimination trap
        missing = [observed("payment.error_log", "present"),
                   observed("payment.restart", "absent"),
                   observed("payment.oom", "absent")]
        assert m.soft_support(missing) < m.soft_support(present)  # weakened
        assert m.hard_incompatibility(missing) is False           # but never eliminated


class TestTemplates:
    def test_process_dead_model_shape(self):
        m = process_dead_model("payment")
        assert m.predictions()["payment.error_log"] == "present"
        assert any(c.observable_id == "payment.health" for c in m.contradictions)

    def test_edge_network_failure_model_shape(self):
        m = edge_network_failure_model("web", "payment")
        assert "web->payment.conn_error" in m.predictions()
        assert m.hard_incompatibility([observed("web->payment.connectivity", "healthy")]) is True


class TestHypothesisIntegration:
    def test_from_observation_model_wires_predictions_and_delegation(self):
        m = process_dead_model("payment")
        h = from_observation_model("process:payment", Kind.PROCESS, "payment", m)
        assert h.predictions == m.predictions()
        assert h.observation_model is m
        assert h.hard_incompatibility([observed("payment.health", "healthy")]) is True
        assert h.soft_support([observed("payment.error_log", "present")]) > 0.0

    def test_bare_hypothesis_has_inert_phase_c_behaviour(self):
        h = from_observation_model("process:p", Kind.PROCESS, "p", ObservationModel())
        assert h.hard_incompatibility([observed("f", "present")]) is False
        assert h.soft_support([observed("f", "present")]) == 0.0

    def test_observation_model_type_is_validated(self):
        from src.core.rca.hypothesis import Hypothesis
        with pytest.raises(ValueError):
            Hypothesis(id="p", kind=Kind.PROCESS, localization="p",
                       observation_model="not-a-model")  # type: ignore[arg-type]
