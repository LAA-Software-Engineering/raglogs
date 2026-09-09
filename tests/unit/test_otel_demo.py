"""Unit tests for the OTel-Demo incident generator (#79). No cluster, no network —
the flag→case logic and the orchestration loop are exercised with fakes."""
from datetime import datetime, timezone

import pytest

from src.eval.otel_demo import (
    CHAOS_SCENARIOS,
    FLAG_SCENARIOS,
    SCENARIOS_BY_CHAOS,
    SCENARIOS_BY_FLAG,
    build_deploy_case,
    build_incident_case,
    generate_chaos_incident,
    generate_deploy_incident,
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


class TestBuildDeployCase:
    def test_deploy_trigger_and_root_cause(self):
        doc = build_deploy_case("dep_1", T0, service="cart", from_tag="v1.0", to_tag="v1.1",
                                baseline_seconds=300, post_seconds=600)
        assert doc["root_cause"]["service"] == "cart"
        assert doc["trigger"]["type"] == "deploy"
        assert doc["trigger"]["timestamp"] == T0.isoformat()
        assert doc["expect_explanation"] is True
        assert "v1.0->v1.1" in doc["notes"]


class TestGenerateDeployIncident:
    def test_rolls_forward_then_back_and_emits_deploy_case(self, tmp_path):
        from src.eval.case import load_case

        rolls: list[str] = []
        out = generate_deploy_incident(
            tmp_path / "dep_1", "dep_1", service="cart", from_tag="v1.0", to_tag="v1.1",
            roll=lambda tag: rolls.append(tag),
            capture=lambda ws, we: ([{"timestamp": T0.isoformat(), "service": "cart", "message": "err", "level": "error"}], [], []),
            sleep=lambda _s: None, now=lambda: T0, baseline_seconds=300, post_seconds=600,
        )
        assert rolls == ["v1.1", "v1.0"]  # forward to regressed tag, then roll back
        case = load_case(out)
        assert case.trigger.type == "deploy"
        assert case.root_cause.service == "cart"


class TestChaos:
    def test_scenarios_valid(self):
        valid = {"deploy", "config", "dependency", "resource", "code", "none"}
        assert CHAOS_SCENARIOS and set(SCENARIOS_BY_CHAOS) == {s.name for s in CHAOS_SCENARIOS}
        for s in CHAOS_SCENARIOS:
            assert s.trigger_type in valid and s.service and s.kind

    def test_generate_applies_then_deletes_and_emits_case(self, tmp_path):
        from src.eval.case import load_case

        events: list[str] = []
        s = SCENARIOS_BY_CHAOS["cartRedisPartition"]
        out = generate_chaos_incident(
            tmp_path / "chaos_1", "chaos_1", scenario=s,
            apply_chaos=lambda sc: events.append(f"apply:{sc.name}"),
            delete_chaos=lambda sc: events.append(f"delete:{sc.name}"),
            capture=lambda ws, we: ([{"timestamp": T0.isoformat(), "service": "cart", "message": "e", "level": "error"}], [], []),
            sleep=lambda _s: None, now=lambda: T0, baseline_seconds=300, post_seconds=600,
        )
        assert events == ["apply:cartRedisPartition", "delete:cartRedisPartition"]
        case = load_case(out)
        assert case.root_cause.service == "cart"
        assert case.trigger.type == "dependency"

    def test_chaos_experiment_deleted_even_if_capture_fails(self, tmp_path):
        import pytest as _pytest

        events: list[str] = []
        s = SCENARIOS_BY_CHAOS["checkoutPodKill"]

        def boom(ws, we):
            raise RuntimeError("capture failed")

        with _pytest.raises(RuntimeError):
            generate_chaos_incident(
                tmp_path / "chaos_2", "chaos_2", scenario=s,
                apply_chaos=lambda sc: events.append("apply"),
                delete_chaos=lambda sc: events.append("delete"),
                capture=boom, sleep=lambda _s: None, now=lambda: T0,
            )
        assert events == ["apply", "delete"]  # cleanup ran despite the failure


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

    def test_confounder_fires_and_is_recorded(self, tmp_path):
        from src.eval.case import load_case

        actions: list[str] = []
        s = SCENARIOS_BY_FLAG["cartServiceFailure"]
        out = generate_incident(
            tmp_path / "cart_conf_1", "cart_conf_1", scenario=s,
            capture=lambda ws, we: ([{"timestamp": T0.isoformat(), "service": "cart", "message": "e", "level": "error"}], [], []),
            flip=lambda f, v: actions.append(f"flag:{f}={v}"),
            sleep=lambda _s: None, now=lambda: T0,
            confounder=lambda: actions.append("unrelated-deploy"), confounder_after=60,
        )
        assert "unrelated-deploy" in actions
        case = load_case(out)
        assert case.trigger.type == "code"  # ground truth stays the flag, not the deploy
        assert "confounder" in case.notes
