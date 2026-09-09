"""Unit tests for telemetry ingestion helpers (#118). No database — deterministic
ids are pure, and persistence is checked against a mock session (idempotency
behaviour on a live DB is an integration concern)."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.core.ingestion.telemetry import (
    ParsedMetricSample,
    ParsedSpan,
    metric_pk,
    persist_metric_samples,
    persist_spans,
    span_pk,
)

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestDeterministicIds:
    def test_span_pk_is_deterministic(self):
        s = ParsedSpan(trace_id="t", span_id="s", service="svc", start_time=T)
        assert span_pk("sc", s) == span_pk("sc", s)

    def test_span_pk_varies_by_scope_and_span(self):
        s = ParsedSpan(trace_id="t", span_id="s", service="svc", start_time=T)
        s2 = ParsedSpan(trace_id="t", span_id="s2", service="svc", start_time=T)
        assert span_pk("sc", s) != span_pk("other", s)
        assert span_pk("sc", s) != span_pk("sc", s2)

    def test_metric_pk_is_deterministic_and_keyed(self):
        m = ParsedMetricSample(service="svc", metric="cpu", value=0.5, ts=T)
        assert metric_pk("sc", m) == metric_pk("sc", m)
        m2 = ParsedMetricSample(service="svc", metric="mem", value=0.5, ts=T)
        assert metric_pk("sc", m) != metric_pk("sc", m2)


class TestPersist:
    def test_persist_spans_builds_deterministic_rows_and_uses_on_conflict(self):
        db = MagicMock()
        spans = [ParsedSpan(trace_id="t", span_id=f"s{i}", service="svc", start_time=T) for i in range(3)]
        n = persist_spans(db, spans, scope="eval:x")
        assert n == 3
        assert db.execute.called
        # the compiled statement is an INSERT ... ON CONFLICT DO NOTHING
        stmt = str(db.execute.call_args[0][0]).lower()
        assert "insert into trace_spans" in stmt
        assert "on conflict" in stmt

    def test_persist_metrics_returns_count_and_inserts(self):
        db = MagicMock()
        samples = [ParsedMetricSample(service="svc", metric="cpu", value=float(i), ts=T) for i in range(5)]
        n = persist_metric_samples(db, samples, scope="eval:x")
        assert n == 5
        stmt = str(db.execute.call_args[0][0]).lower()
        assert "insert into metric_samples" in stmt
        assert "on conflict" in stmt

    def test_empty_is_a_noop(self):
        db = MagicMock()
        assert persist_spans(db, [], scope="s") == 0
        assert persist_metric_samples(db, [], scope="s") == 0
        db.execute.assert_not_called()
