from datetime import datetime, timezone

import pytest
from src.core.clustering.baseline import compute_change_ratio
from src.core.clustering.clusterer import _CLUSTER_ROW_COLUMNS, _cluster_select, _group_rows
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


class TestGroupRows:
    """_group_rows unpacks the clustering projection positionally (the #85 hot-path
    optimization); these tests pin that the column order maps to the right aggregate,
    so a future reordering of the SELECT can't silently corrupt clusters."""

    def _row(self, fp, msg, service, level, ts, entry_id):
        # Build in the documented column order so the test fails if either the
        # helper's unpack or _CLUSTER_ROW_COLUMNS drifts.
        return (fp, msg, service, level, ts, entry_id)

    def test_column_contract_is_the_documented_order(self):
        assert _CLUSTER_ROW_COLUMNS == (
            "fingerprint", "normalized_message", "service", "level", "timestamp", "id",
        )

    def test_real_query_column_order_matches_the_contract(self):
        # Assert against the ACTUAL statement the clusterer runs, not a hand-copied
        # literal: a reorder of the columns in _cluster_select() (e.g. swapping
        # service/level) changes selected_columns and fails here, which is the whole
        # point of the positional-unpack contract. selected_columns.keys() is the
        # compiled column order of the projection.
        assert tuple(_cluster_select().selected_columns.keys()) == _CLUSTER_ROW_COLUMNS

    def test_groups_by_fingerprint_with_correct_aggregates(self):
        t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 1, 12, 9, tzinfo=timezone.utc)
        # service and level are deliberately disjoint vocabularies: a positional
        # swap would land "api" in levels or "error" in services and fail below.
        rows = [
            self._row("fp1", "boom", "api", "error", t1, 1),
            self._row("fp1", "boom", "api", "error", t0, 2),
            self._row("fp1", "boom", "web", "warn", t2, 3),
            self._row("fp2", "ok", "worker", "info", t1, 4),
        ]
        groups = _group_rows(rows)

        assert set(groups) == {"fp1", "fp2"}
        fp1 = groups["fp1"]
        assert fp1["ids"] == [1, 2, 3]
        assert dict(fp1["services"]) == {"api": 2, "web": 1}
        assert dict(fp1["levels"]) == {"error": 2, "warn": 1}
        # only error/fatal/critical lines count toward error_services
        assert dict(fp1["error_services"]) == {"api": 2}
        assert min(fp1["timestamps"]) == t0
        assert max(fp1["timestamps"]) == t2
        assert fp1["messages"] == ["boom", "boom", "boom"]

        fp2 = groups["fp2"]
        assert fp2["ids"] == [4]
        assert dict(fp2["error_services"]) == {}

    def test_none_fields_are_skipped(self):
        rows = [
            ("fp", None, None, None, None, 10),  # all-None but id
            ("fp", "m", "svc", "info", datetime(2026, 1, 1, tzinfo=timezone.utc), 11),
        ]
        groups = _group_rows(rows)
        g = groups["fp"]
        assert g["ids"] == [10, 11]           # id is always recorded
        assert g["messages"] == ["m"]          # None message skipped
        assert dict(g["services"]) == {"svc": 1}
        assert dict(g["levels"]) == {"info": 1}
        assert len(g["timestamps"]) == 1       # None timestamp skipped

    def test_empty_input(self):
        assert _group_rows([]) == {}
