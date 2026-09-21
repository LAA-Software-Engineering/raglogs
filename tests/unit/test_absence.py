"""Phase F (#184) — absence-derived candidate detection. Pure, no DB."""

from datetime import datetime, timedelta

from src.core.rca.features import detect_absence

_BASE = datetime(2026, 1, 1, 11, 59)   # baseline_start
_INC = datetime(2026, 1, 1, 12, 0)     # incident_start (= window start)
_END = datetime(2026, 1, 1, 12, 2)     # incident_end


class _Span:
    def __init__(self, service, ts):
        self.service, self.start_time = service, ts


def _spans(service, *, baseline, incident):
    out = []
    for i in range(baseline):
        out.append(_Span(service, _BASE + timedelta(seconds=i * 0.5)))
    for i in range(incident):
        out.append(_Span(service, _INC + timedelta(seconds=i * 0.5)))
    return out


class TestDetectAbsence:
    def test_baselined_then_vanished_is_a_candidate(self):
        spans = _spans("payment", baseline=60, incident=0)  # present in baseline, silent in incident
        absent = detect_absence(spans, _BASE, _INC, _END)
        assert "payment" in absent and absent["payment"] > 0.9  # near-total collapse

    def test_missing_telemetry_is_not_a_disappearance(self):
        # a service never seen (no baseline spans) must never be an absence candidate (Invariant 2)
        spans = _spans("healthy", baseline=60, incident=60)
        assert "never-seen" not in detect_absence(spans, _BASE, _INC, _END)

    def test_insufficient_baseline_is_not_a_candidate(self):
        # only a couple of baseline spans -> the signal was not established as "expected"
        spans = _spans("blip", baseline=3, incident=0)
        assert detect_absence(spans, _BASE, _INC, _END) == {}

    def test_steady_traffic_is_not_a_collapse(self):
        spans = _spans("steady", baseline=60, incident=120)  # rate roughly maintained
        assert "steady" not in detect_absence(spans, _BASE, _INC, _END)

    def test_partial_collapse_below_fraction_is_a_candidate(self):
        # incident rate drops to ~5% of baseline rate -> collapse
        spans = _spans("fading", baseline=120, incident=3)
        absent = detect_absence(spans, _BASE, _INC, _END)
        assert "fading" in absent

    def test_empty_when_nothing_disappeared(self):
        spans = _spans("a", baseline=60, incident=60) + _spans("b", baseline=40, incident=50)
        assert detect_absence(spans, _BASE, _INC, _END) == {}
