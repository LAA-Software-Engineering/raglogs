"""Unit tests for the OTel-Demo incident generator (#79). No cluster, no network —
the flag→case logic and the orchestration loop are exercised with fakes."""
from datetime import datetime, timezone

import pytest

from src.eval.otel_demo import (
    FLAG_SCENARIOS,
    SCENARIOS_BY_FLAG,
    build_incident_case,
    generate_incident,
    patch_flag_variant,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestScenarios:
    def test_every_scenario_has_valid_trigger_type(self):
        valid = {"deploy", "config", "dependency", "resource", "code", "none"}
        assert FLAG_SCENARIOS  # non-empty
        for s in FLAG_SCENARIOS:
            assert s.trigger_type in valid
            assert s.service and s.flag
        assert set(SCENARIOS_BY_FLAG) == {s.flag for s in FLAG_SCENARIOS}


class TestPatchFlagVariant:
    def test_patches_only_the_target_flag(self):
        cfg = {"flags": {"paymentServiceFailure": {"defaultVariant": "off", "state": "ENABLED"},
                         "adHighCpu": {"defaultVariant": "off"}}}
        out = patch_flag_variant(cfg, "paymentServiceFailure", "on")
        assert out["flags"]["paymentServiceFailure"]["defaultVariant"] == "on"
        assert out["flags"]["paymentServiceFailure"]["state"] == "ENABLED"  # other keys preserved
        assert out["flags"]["adHighCpu"]["defaultVariant"] == "off"  # untouched
        assert cfg["flags"]["paymentServiceFailure"]["defaultVariant"] == "off"  # input not mutated

    def test_unknown_flag_raises(self):
        with pytest.raises(KeyError):
            patch_flag_variant({"flags": {}}, "nope", "on")


class TestBuildIncidentCase:
    def test_positive_case_has_root_cause_and_trigger(self):
        s = SCENARIOS_BY_FLAG["paymentServiceFailure"]
        doc = build_incident_case("payment_1", T0, scenario=s, baseline_seconds=300, post_seconds=600)
        assert doc["root_cause"]["service"] == "payment"
        assert doc["trigger"]["timestamp"] == T0.isoformat()
        assert doc["trigger"]["type"] == "code"
        assert doc["expect_explanation"] is True
        assert doc["window"]["start"] == T0.isoformat()  # incident starts at the flip
        assert doc["baseline"] == "300s"

    def test_negative_case_abstains(self):
        doc = build_incident_case("healthy_1", T0, scenario=None, baseline_seconds=300, post_seconds=600)
        assert doc["expect_explanation"] is False
        assert "root_cause" not in doc and "trigger" not in doc
        assert "healthy negative" in doc["notes"]

    def test_confounder_recorded_but_ground_truth_is_the_flag(self):
        s = SCENARIOS_BY_FLAG["cartServiceFailure"]
        deploy = datetime(2026, 1, 1, 12, 3, tzinfo=timezone.utc)
        doc = build_incident_case("cart_conf_1", T0, scenario=s, baseline_seconds=300,
                                  post_seconds=600, confounding_deploy_at=deploy)
        assert doc["trigger"]["timestamp"] == T0.isoformat()  # the flag flip, not the deploy
        assert "confounder" in doc["notes"] and deploy.isoformat() in doc["notes"]


class TestGenerateIncident:
    def test_loop_flips_records_and_flips_back(self, tmp_path):
        from src.eval.case import load_case

        flips: list[tuple[str, str]] = []
        captured_windows: list[tuple] = []

        def capture(ws, we):
            captured_windows.append((ws, we))
            return ([{"timestamp": T0.isoformat(), "service": "payment", "message": "boom", "level": "error"}], [], [])

        s = SCENARIOS_BY_FLAG["paymentServiceFailure"]
        out = generate_incident(
            tmp_path / "payment_1", "payment_1", scenario=s,
            capture=capture, flip=lambda f, v: flips.append((f, v)),
            sleep=lambda _s: None, now=lambda: T0,
            baseline_seconds=300, post_seconds=600,
        )
        # flag flipped on before capture, off after
        assert flips == [("paymentServiceFailure", "on"), ("paymentServiceFailure", "off")]
        # the captured window spans baseline..incident around the flip
        assert captured_windows[0][0] < T0 < captured_windows[0][1]
        case = load_case(out)
        assert case.root_cause.service == "payment"
        assert case.trigger.type == "code"
        assert case.logs_paths  # logs.jsonl written

    def test_negative_case_flips_nothing(self, tmp_path):
        from src.eval.case import load_case

        flips: list = []
        out = generate_incident(
            tmp_path / "healthy_1", "healthy_1", scenario=None,
            capture=lambda ws, we: ([], [], []),
            flip=lambda f, v: flips.append((f, v)),
            sleep=lambda _s: None, now=lambda: T0,
        )
        assert flips == []  # no flag touched on a healthy case
        assert load_case(out).expect_explanation is False
