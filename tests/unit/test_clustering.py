import pytest
from src.core.clustering.baseline import compute_change_ratio
from src.core.clustering.scoring import compute_importance_score, get_severity_weight


class TestChangeRatio:
    def test_no_baseline(self):
        ratio = compute_change_ratio(100, 0)
        assert ratio == pytest.approx(101.0)  # (100+1)/(0+1)

    def test_same_count(self):
        ratio = compute_change_ratio(10, 10)
        assert ratio == pytest.approx(1.0)  # (11)/(11)

    def test_zero_both(self):
        ratio = compute_change_ratio(0, 0)
        assert ratio == pytest.approx(1.0)  # (1)/(1)

    def test_increase(self):
        ratio = compute_change_ratio(184, 1)
        assert ratio > 50  # large spike


class TestSeverityWeight:
    def test_error_higher_than_warn(self):
        error_w = get_severity_weight({"error": 10})
        warn_w = get_severity_weight({"warn": 10})
        assert error_w > warn_w

    def test_fatal_highest(self):
        fatal_w = get_severity_weight({"fatal": 1})
        error_w = get_severity_weight({"error": 1})
        assert fatal_w > error_w

    def test_mixed_distribution(self):
        w = get_severity_weight({"error": 5, "warn": 5})
        assert 3.0 < w < 4.0  # between warn and error

    def test_empty(self):
        w = get_severity_weight({})
        assert w == 1.0


class TestImportanceScore:
    def test_error_spike_scores_high(self):
        score = compute_importance_score(
            count=184,
            levels_distribution={"error": 184},
            change_ratio=184.0,
            services_count=1,
            is_trigger_correlated=True,
        )
        assert score > 10

    def test_trigger_boost(self):
        base = compute_importance_score(
            count=10, levels_distribution={"error": 10},
            change_ratio=2.0, services_count=1, is_trigger_correlated=False
        )
        with_trigger = compute_importance_score(
            count=10, levels_distribution={"error": 10},
            change_ratio=2.0, services_count=1, is_trigger_correlated=True
        )
        assert with_trigger > base

    def test_multi_service_boost(self):
        single = compute_importance_score(
            count=10, levels_distribution={"error": 10},
            change_ratio=5.0, services_count=1
        )
        multi = compute_importance_score(
            count=10, levels_distribution={"error": 10},
            change_ratio=5.0, services_count=3
        )
        assert multi > single
