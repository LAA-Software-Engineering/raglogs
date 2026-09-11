"""Unit tests for the abstention gate math (#79). No DB — pure functions.

Covers the design contract: stabilize-then-saturate, non-negativity clamp, max
fusion over *available* modalities, and no-modality → abstain."""
import math

import pytest

from src.core.rca.abstention import (
    WindowAnomaly,
    log_rate_anomaly,
    magnitude_anomaly,
    ratio_anomaly,
    saturate,
    should_abstain,
    window_anomaly,
)


class TestSaturate:
    def test_zero_and_monotone_bounded(self):
        assert saturate(0.0, 1.0) == 0.0
        assert saturate(-5.0, 1.0) == 0.0  # clamped: below-baseline is not negative
        assert 0.0 < saturate(1.0, 1.0) < saturate(5.0, 1.0) < 1.0  # monotone, bounded
        assert saturate(1.0, 1.0) == pytest.approx(1 - math.exp(-1))

    def test_tau_sets_the_scale(self):
        # larger tau -> same x saturates less
        assert saturate(1.0, 5.0) < saturate(1.0, 1.0)

    def test_nonpositive_tau_degenerates(self):
        assert saturate(0.0, 0.0) == 0.0
        assert saturate(0.1, 0.0) == 1.0  # any positive anomaly saturates


class TestEffectSizes:
    def test_log_rate_clamps_below_baseline(self):
        # incident quieter than baseline -> no anomaly (not negative)
        assert log_rate_anomaly(incident_rate=1.0, baseline_rate=50.0, tau=1.0) == 0.0

    def test_log_rate_stabilizes_near_zero_baseline(self):
        # a huge incident rate against ~0 baseline does NOT saturate to ~1 blindly:
        # log1p keeps the effect size finite and tau-scaled.
        s = log_rate_anomaly(incident_rate=1e6, baseline_rate=0.0, tau=1.0)
        # log1p(1e6) ~= 13.8 -> saturates high, but via the stabilized effect size
        assert 0.9 < s < 1.0
        # a modest elevation is clearly mid-range, not blown out
        s2 = log_rate_anomaly(incident_rate=3.0, baseline_rate=1.0, tau=1.0)
        assert 0.0 < s2 < s

    def test_ratio_anomaly_elevation_only(self):
        assert ratio_anomaly(0.5, tau=1.0) == 0.0  # below 1.0 -> no elevation
        assert ratio_anomaly(1.0, tau=1.0) == 0.0
        assert 0.0 < ratio_anomaly(math.e, tau=1.0) < 1.0  # log(e)=1

    def test_magnitude_anomaly(self):
        assert magnitude_anomaly(0.0, tau=1.0) == 0.0
        assert magnitude_anomaly(-2.0, tau=1.0) == 0.0  # clamped
        assert magnitude_anomaly(2.0, tau=1.0) == pytest.approx(1 - math.exp(-2))


class TestFusion:
    def test_max_over_available(self):
        a = window_anomaly(logs=0.3, traces=0.8, metrics=0.1)
        assert a.score == 0.8 and set(a.available) == {"logs", "traces", "metrics"}

    def test_missing_modality_dropped_not_zero(self):
        # traces absent (None) must not read as 0 "all clear"
        a = window_anomaly(logs=0.6, traces=None, metrics=None)
        assert a.score == 0.6 and a.available == ("logs",)
        assert a.has_evidence is True

    def test_no_modalities_scores_zero_and_no_evidence(self):
        a = window_anomaly()
        assert a.score == 0.0 and a.available == () and a.has_evidence is False


class TestShouldAbstain:
    def test_abstains_below_threshold(self):
        assert should_abstain(WindowAnomaly(0.4, ("logs",)), threshold=0.5) is True
        assert should_abstain(WindowAnomaly(0.6, ("logs",)), threshold=0.5) is False

    def test_no_evidence_always_abstains(self):
        # even a threshold of 0 abstains when there is no available modality
        assert should_abstain(WindowAnomaly(0.0, ()), threshold=0.0) is True
