"""Phase F (#184) — absence-derived candidate detection. Pure, no DB.

The availability gate is a *failed-attempt edge* signal: a collapsed service is a disappearance
candidate only when a baseline caller of it recorded an **error-status span in the incident** — the
truncated-trace "couldn't reach the downstream" record. Mere caller liveness is not enough: a caller
that keeps working *without erroring toward the callee* (the callee's own exporter failed while it
still served, or the caller simply stopped calling it) leaves the callee UNKNOWN, never absent.
"""

from datetime import datetime, timedelta

from src.core.rca.features import detect_absence

_BASE = datetime(2026, 1, 1, 11, 59)   # baseline_start (1 min baseline)
_INC = datetime(2026, 1, 1, 12, 0)     # incident_start (= window start)
_END = datetime(2026, 1, 1, 12, 2)     # incident_end (2 min incident)


class _Span:
    def __init__(self, service, ts, *, span_id=None, parent_span_id=None, trace_id="t",
                 status_code="0"):
        self.service, self.start_time = service, ts
        self.span_id, self.parent_span_id, self.trace_id = span_id, parent_span_id, trace_id
        self.status_code = status_code


def _edge_spans(caller, callee, ts, i, *, caller_only=False, caller_error=False):
    """One caller->callee trace (or caller-only when the callee has vanished). Ids are unique per
    (caller, callee) stream so independent traffic streams never collide. ``caller_error`` marks the
    caller span with OTel ERROR status (2) — the trace record of a failed call to the missing callee."""
    tag = f"{caller}-{callee}-{i}"
    cid, pid, tr = f"c{tag}", f"p{tag}", f"tr{tag}"
    out = [_Span(caller, ts, span_id=cid, trace_id=tr, status_code="2" if caller_error else "0")]
    if not caller_only:
        out.append(_Span(callee, ts + timedelta(milliseconds=2), span_id=pid, parent_span_id=cid,
                         trace_id=tr))
    return out


def _traffic(caller, callee, *, baseline, incident, base_start=_BASE, incident_caller_only=True,
             incident_caller_error=True):
    spans = []
    step = (_INC - base_start).total_seconds() / max(baseline, 1)
    for i in range(baseline):  # baseline: full, healthy caller->callee edge
        spans += _edge_spans(caller, callee, base_start + timedelta(seconds=i * step), i)
    for i in range(incident):
        spans += _edge_spans(caller, callee, _INC + timedelta(seconds=i * 0.5), 1000 + i,
                             caller_only=incident_caller_only, caller_error=incident_caller_error)
    return spans


class TestDetectAbsence:
    def test_baselined_callee_vanishes_while_caller_fails_toward_it(self):
        # checkout -> payment in the baseline; in the incident payment vanishes and checkout records
        # an error toward it (the failed-attempt edge) -> payment is a candidate.
        spans = _traffic("checkout", "payment", baseline=60, incident=60)
        absent = detect_absence(spans, _BASE, _INC, _END)
        assert "payment" in absent and absent["payment"] > 0.9

    def test_no_active_caller_stays_unknown(self):
        # gate 2: the whole path went dark (checkout also stopped) -> no failed-attempt edge on
        # payment's path this incident -> UNKNOWN, not a fabricated disappearance.
        spans = _traffic("checkout", "payment", baseline=60, incident=0)
        assert detect_absence(spans, _BASE, _INC, _END) == {}

    def test_caller_succeeds_silently_is_not_absence(self):
        # Reviewer counterexample: payment's own exporter failed while it kept serving, so checkout
        # called it *successfully* (no error). Collapse is real but there is no failed-attempt edge ->
        # this is missing telemetry, which must stay UNKNOWN (Invariant 2), not a causal candidate.
        spans = _traffic("checkout", "payment", baseline=60, incident=60, incident_caller_error=False)
        assert "payment" not in detect_absence(spans, _BASE, _INC, _END)

    def test_caller_stops_calling_without_error_is_not_absence(self):
        # Reviewer counterexample: checkout keeps working but stops calling payment while doing other
        # work (non-error incident spans, no payment child). No error toward payment -> UNKNOWN.
        spans = _traffic("checkout", "payment", baseline=60, incident=60,
                         incident_caller_only=True, incident_caller_error=False)
        assert "payment" not in detect_absence(spans, _BASE, _INC, _END)

    def test_unrelated_failing_service_does_not_confirm_absence(self):
        # An unrelated *erroring* service (web->cache) is not on payment's edge; checkout stopped, so
        # nothing failed toward payment -> payment stays UNKNOWN despite errors elsewhere.
        spans = _traffic("checkout", "payment", baseline=60, incident=0)
        spans += _traffic("web", "cache", baseline=60, incident=60, incident_caller_only=False,
                          incident_caller_error=True)
        assert "payment" not in detect_absence(spans, _BASE, _INC, _END)

    def test_missing_telemetry_is_not_a_disappearance(self):
        spans = _traffic("checkout", "payment", baseline=60, incident=60)
        assert "never-seen" not in detect_absence(spans, _BASE, _INC, _END)

    def test_insufficient_baseline_is_not_a_candidate(self):
        spans = _traffic("checkout", "payment", baseline=3, incident=60)
        assert "payment" not in detect_absence(spans, _BASE, _INC, _END)

    def test_steady_traffic_is_not_a_collapse(self):
        spans = _traffic("checkout", "payment", baseline=60, incident=120, incident_caller_only=False)
        assert "payment" not in detect_absence(spans, _BASE, _INC, _END)

    def test_sparse_over_long_baseline_is_not_a_collapse(self):
        # gate 3: 5 payment spans over a 24h baseline predict ~zero incident spans; zero is normal.
        long_base = _INC - timedelta(hours=24)
        spans = _traffic("sched", "cron", baseline=5, incident=60, base_start=long_base)
        assert "cron" not in detect_absence(spans, long_base, _INC, _END)
