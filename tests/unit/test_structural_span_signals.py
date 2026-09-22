"""#209 M1 — per-service sig derived from spans (latency-first + sparse error-status), and the
span+metric combiner. Pure, no DB. Enforces the UNKNOWN discipline: missing telemetry never becomes
a fabricated ABSENT/PRESENT."""
from datetime import datetime, timedelta, timezone

from src.core.rca.observable import State
from src.core.rca.structural_model import (
    ServiceSignal,
    combine_signals,
    summarize_spans,
)

_W = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)  # window_start


class _Span:
    def __init__(self, service, ts, duration_ms=10.0, status_code=None):
        self.service, self.start_time = service, ts
        self.duration_ms, self.status_code = duration_ms, status_code


def _stream(service, *, base_ms, base_n, inc_ms, inc_n, inc_err=0):
    """`base_n` baseline spans at `base_ms`, `inc_n` incident spans at `inc_ms`, `inc_err` of them error."""
    out = []
    for i in range(base_n):
        out.append(_Span(service, _W - timedelta(seconds=i + 1), duration_ms=base_ms))
    for i in range(inc_n):
        out.append(_Span(service, _W + timedelta(seconds=i), duration_ms=inc_ms,
                         status_code="2" if i < inc_err else None))
    return out


class TestSummarizeSpans:
    def test_latency_excursion_is_present(self):
        sig = summarize_spans(_stream("api", base_ms=10, base_n=20, inc_ms=40, inc_n=20), _W)["api"]
        assert sig.sig_state == State.PRESENT and sig.latency_measured

    def test_error_status_is_present(self):
        # steady latency but 20% of incident spans carry ERROR status -> present via the error branch.
        sig = summarize_spans(_stream("api", base_ms=10, base_n=20, inc_ms=10, inc_n=20, inc_err=6), _W)["api"]
        assert sig.sig_state == State.PRESENT

    def test_measured_normal_is_absent(self):
        # both branches measured and normal (latency ~flat, no errors) -> proven ABSENT.
        sig = summarize_spans(_stream("api", base_ms=10, base_n=20, inc_ms=11, inc_n=20), _W)["api"]
        assert sig.sig_state == State.ABSENT

    def test_no_baseline_durations_stays_unknown(self):
        # incident spans only (no baseline) -> latency branch unmeasured, error normal -> UNKNOWN,
        # never a fabricated ABSENT (Invariant: missing telemetry is not absence).
        sig = summarize_spans(_stream("api", base_ms=10, base_n=0, inc_ms=10, inc_n=20), _W)["api"]
        assert sig.sig_state is None and sig.error_measured and not sig.latency_measured

    def test_never_seen_service_absent_from_signals(self):
        signals = summarize_spans(_stream("api", base_ms=10, base_n=20, inc_ms=40, inc_n=20), _W)
        assert "ghost" not in signals

    def test_invalid_durations_are_ignored_not_averaged(self):
        # a NaN/negative duration must not corrupt the latency branch (it is dropped, not averaged in).
        spans = _stream("api", base_ms=10, base_n=20, inc_ms=40, inc_n=20)
        spans.append(_Span("api", _W + timedelta(seconds=99), duration_ms=float("nan")))
        spans.append(_Span("api", _W + timedelta(seconds=98), duration_ms=-5.0))
        sig = summarize_spans(spans, _W)["api"]
        assert sig.sig_state == State.PRESENT  # still a clean latency excursion


def _sig(service, state, *, err_m=True, lat_m=True):
    return ServiceSignal(service, 0.0, 1.0, err_m, lat_m, state)


class TestCombineSignals:
    def test_present_wins(self):
        merged = combine_signals({"a": _sig("a", State.ABSENT)}, {"a": _sig("a", State.PRESENT)})
        assert merged["a"].sig_state == State.PRESENT

    def test_absent_over_unknown(self):
        merged = combine_signals({"a": _sig("a", None)}, {"a": _sig("a", State.ABSENT)})
        assert merged["a"].sig_state == State.ABSENT

    def test_all_unknown_stays_unknown(self):
        merged = combine_signals({"a": _sig("a", None)}, {"a": _sig("a", None)})
        assert merged["a"].sig_state is None  # never fabricated to ABSENT

    def test_union_of_services(self):
        merged = combine_signals({"a": _sig("a", State.PRESENT)}, {"b": _sig("b", State.ABSENT)})
        assert set(merged) == {"a", "b"}
