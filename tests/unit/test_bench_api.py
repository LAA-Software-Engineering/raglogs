"""Unit tests for the API load-test harness pure logic (no Postgres, no app)."""

import pytest
from scripts.bench_api import _percentile


class TestPercentile:
    def test_empty(self):
        assert _percentile([], 0.5) == 0.0

    def test_single(self):
        assert _percentile([42.0], 0.99) == 42.0

    def test_min_and_max(self):
        vals = [10.0, 20.0, 30.0, 40.0]
        assert _percentile(vals, 0.0) == 10.0
        assert _percentile(vals, 1.0) == 40.0

    def test_p50_interpolates(self):
        # k = (4-1)*0.5 = 1.5 -> between index 1 (20) and 2 (30)
        assert _percentile([10.0, 20.0, 30.0, 40.0], 0.5) == pytest.approx(25.0)

    def test_p95_interpolates(self):
        # k = 3*0.95 = 2.85 -> 30 + (40-30)*0.85
        assert _percentile([10.0, 20.0, 30.0, 40.0], 0.95) == pytest.approx(38.5)

    def test_expects_sorted_input(self):
        # _percentile assumes its input is already sorted (the caller sorts once);
        # a monotonic list gives monotonic percentiles.
        vals = [float(i) for i in range(100)]
        assert _percentile(vals, 0.5) < _percentile(vals, 0.9) < _percentile(vals, 0.99)
