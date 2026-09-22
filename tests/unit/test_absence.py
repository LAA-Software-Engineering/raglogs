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


def _detect(spans, available, **kw):
    return detect_absence(spans, _BASE, _INC, _END, available_services=set(available), **kw)


class TestDetectAbsence:
    def test_baselined_then_vanished_is_a_candidate(self):
        # payment's spans collapsed, but it is independently available (still emitting metrics)
        absent = _detect(_spans("payment", baseline=60, incident=0), available={"payment"})
        assert "payment" in absent and absent["payment"] > 0.9

    def test_target_not_independently_available_stays_unknown(self):
        # gate 2: payment span-collapsed AND not in available (its metrics also stopped) -> the whole
        # collection path/instrumentation failed; indistinguishable from disappearance, so UNKNOWN.
        assert _detect(_spans("payment", baseline=60, incident=0), available=set()) == {}

    def test_unrelated_healthy_service_does_not_make_target_available(self):
        # an unrelated healthy service emitting is NOT evidence the target was measured (scope-wide
        # activity is insufficient): payment absent from `available` -> not diagnosed.
        spans = _spans("payment", baseline=60, incident=0) + _spans("healthy", baseline=60, incident=120)
        assert "payment" not in _detect(spans, available={"healthy"})

    def test_missing_telemetry_is_not_a_disappearance(self):
        spans = _spans("healthy", baseline=60, incident=60)
        assert "never-seen" not in _detect(spans, available={"healthy", "never-seen"})

    def test_insufficient_baseline_is_not_a_candidate(self):
        assert "blip" not in _detect(_spans("blip", baseline=3, incident=0), available={"blip"})

    def test_steady_traffic_is_not_a_collapse(self):
        assert "steady" not in _detect(_spans("steady", baseline=60, incident=120), available={"steady"})

    def test_sparse_service_over_long_baseline_is_not_a_collapse(self):
        # gate 3: 5 spans over a 24h baseline predict ~zero incident spans; zero is normal.
        long_base = _INC - timedelta(hours=24)
        spans = _spans("cron", baseline=5, incident=0, base_start=long_base)
        assert detect_absence(spans, long_base, _INC, _END, available_services={"cron"}) == {}

    def test_empty_when_nothing_disappeared(self):
        spans = _spans("a", baseline=60, incident=60) + _spans("b", baseline=40, incident=50)
        assert _detect(spans, available={"a", "b"}) == {}
