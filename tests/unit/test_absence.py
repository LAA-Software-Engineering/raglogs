"""Phase F (#184, evidence-only) — silent-service detection. Pure, no DB.

`detect_absence` returns services whose span traffic was **baselined then collapsed** in the incident.
This is observed *evidence* ("went silent — unreachable OR a trace-collection gap"), never a causal
candidate: traces alone cannot attribute silence to a failed call toward the service. The gates that
matter here are therefore only about honest collapse detection — baselined (Invariant 2: missing
telemetry is not a disappearance), expected, and a real rate collapse.
"""

from datetime import datetime, timedelta

from src.core.rca.features import detect_absence

_BASE = datetime(2026, 1, 1, 11, 59)   # baseline_start (1 min baseline)
_INC = datetime(2026, 1, 1, 12, 0)     # incident_start (= window start)
_END = datetime(2026, 1, 1, 12, 2)     # incident_end (2 min incident)


class _Span:
    def __init__(self, service, ts):
        self.service, self.start_time = service, ts


def _spans(service, *, baseline, incident, base_start=_BASE):
    """`baseline` spans spread over the baseline window, `incident` over the incident window."""
    out = []
    step = (_INC - base_start).total_seconds() / max(baseline, 1)
    for i in range(baseline):
        out.append(_Span(service, base_start + timedelta(seconds=i * step)))
    for i in range(incident):
        out.append(_Span(service, _INC + timedelta(seconds=i * 0.5)))
    return out


class TestDetectAbsence:
    def test_baselined_service_that_goes_silent(self):
        # payment present all baseline, absent in the incident -> silent-service evidence.
        absent = detect_absence(_spans("payment", baseline=60, incident=0), _BASE, _INC, _END)
        assert "payment" in absent and absent["payment"] > 0.9

    def test_partial_collapse_below_fraction(self):
        # a >80% rate drop still counts as a collapse; a mild dip does not.
        assert "payment" in detect_absence(_spans("payment", baseline=60, incident=2), _BASE, _INC, _END)
        assert "payment" not in detect_absence(_spans("payment", baseline=60, incident=90),
                                               _BASE, _INC, _END)

    def test_missing_telemetry_is_not_a_disappearance(self):
        # never-seen service has no baseline -> cannot "collapse" (Invariant 2).
        assert "never-seen" not in detect_absence(_spans("payment", baseline=60, incident=0),
                                                  _BASE, _INC, _END)

    def test_insufficient_baseline_is_not_a_candidate(self):
        assert "payment" not in detect_absence(_spans("payment", baseline=3, incident=0),
                                               _BASE, _INC, _END)

    def test_steady_traffic_is_not_a_collapse(self):
        assert "payment" not in detect_absence(_spans("payment", baseline=60, incident=120),
                                               _BASE, _INC, _END)

    def test_sparse_over_long_baseline_is_not_a_collapse(self):
        # gate 2: 5 spans over a 24h baseline predict ~zero incident spans; zero is normal.
        long_base = _INC - timedelta(hours=24)
        spans = _spans("cron", baseline=5, incident=0, base_start=long_base)
        assert "cron" not in detect_absence(spans, long_base, _INC, _END)
