"""Phase F (#184) — absence-derived candidate detection. Pure, no DB."""

from datetime import datetime, timedelta

from src.core.rca.features import detect_absence

_BASE = datetime(2026, 1, 1, 11, 59)   # baseline_start (1 min baseline)
_INC = datetime(2026, 1, 1, 12, 0)     # incident_start (= window start)
_END = datetime(2026, 1, 1, 12, 2)     # incident_end (2 min incident)


class _Span:
    def __init__(self, service, ts):
        self.service, self.start_time = service, ts


def _spans(service, *, baseline, incident, base_start=_BASE):
    out = []
    step = (_INC - base_start).total_seconds() / max(baseline, 1)
    for i in range(baseline):
        out.append(_Span(service, base_start + timedelta(seconds=i * step)))
    for i in range(incident):
        out.append(_Span(service, _INC + timedelta(seconds=i * 0.5)))
    return out


# A healthy service that keeps emitting in the incident, so traces are demonstrably available
# (gate 2) — without it, a lone vanished service looks like a whole-collector outage.
_HEALTHY = _spans("healthy", baseline=60, incident=120)


class TestDetectAbsence:
    def test_baselined_then_vanished_is_a_candidate(self):
        spans = _spans("payment", baseline=60, incident=0) + _HEALTHY
        absent = detect_absence(spans, _BASE, _INC, _END)
        assert "payment" in absent and absent["payment"] > 0.9  # near-total collapse

    def test_missing_telemetry_is_not_a_disappearance(self):
        # a service never seen (no baseline spans) must never be an absence candidate (Invariant 2)
        assert "never-seen" not in detect_absence(_HEALTHY, _BASE, _INC, _END)

    def test_insufficient_baseline_is_not_a_candidate(self):
        spans = _spans("blip", baseline=3, incident=0) + _HEALTHY
        assert "blip" not in detect_absence(spans, _BASE, _INC, _END)

    def test_steady_traffic_is_not_a_collapse(self):
        spans = _spans("steady", baseline=60, incident=120) + _HEALTHY
        assert "steady" not in detect_absence(spans, _BASE, _INC, _END)

    def test_collector_outage_is_not_disappearance(self):
        # gate 2: every service loses incident spans -> the collector stopped, not a per-service
        # disappearance. Nothing may be diagnosed (else all baselined services look vanished).
        spans = _spans("a", baseline=60, incident=0) + _spans("b", baseline=60, incident=0)
        assert detect_absence(spans, _BASE, _INC, _END) == {}

    def test_sparse_service_over_long_baseline_is_not_a_collapse(self):
        # gate 3: 5 spans over a 24h baseline predict ~zero spans in the incident; zero is normal.
        long_base = _INC - timedelta(hours=24)
        spans = _spans("cron", baseline=5, incident=0, base_start=long_base) + _HEALTHY
        assert "cron" not in detect_absence(spans, long_base, _INC, _END)

    def test_empty_when_nothing_disappeared(self):
        spans = _spans("a", baseline=60, incident=60) + _spans("b", baseline=40, incident=50)
        assert detect_absence(spans, _BASE, _INC, _END) == {}
