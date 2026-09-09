"""Schema guards for the telemetry tables (#118). No database required —
inspects the SQLAlchemy table metadata so the additive model can't silently
drift from the design / migration.
"""
from src.db.models import MetricSample, TraceSpan


def _cols(model):
    return {c.name: c for c in model.__table__.columns}


class TestTraceSpan:
    def test_tablename(self):
        assert TraceSpan.__tablename__ == "trace_spans"

    def test_has_span_tree_and_timing_columns(self):
        cols = _cols(TraceSpan)
        for name in (
            "trace_id", "span_id", "parent_span_id", "service", "operation",
            "start_time", "duration_ms", "status_code", "attributes",
            "scope", "ingestion_job_id",
        ):
            assert name in cols, f"trace_spans missing {name}"

    def test_scope_not_null_defaults_default(self):
        scope = _cols(TraceSpan)["scope"]
        assert scope.nullable is False
        assert scope.server_default is not None

    def test_indexes_cover_service_time_and_trace(self):
        idx = {i.name for i in TraceSpan.__table__.indexes}
        assert "ix_trace_spans_scope_service_start" in idx
        assert "ix_trace_spans_scope_trace" in idx


class TestMetricSample:
    def test_tablename(self):
        assert MetricSample.__tablename__ == "metric_samples"

    def test_has_long_format_columns(self):
        cols = _cols(MetricSample)
        for name in ("service", "metric", "value", "ts", "attributes", "scope", "ingestion_job_id"):
            assert name in cols, f"metric_samples missing {name}"
        # long format: one metric name per row (not wide {service}_{metric})
        assert cols["metric"].nullable is False

    def test_index_covers_service_metric_ts(self):
        idx = {i.name for i in MetricSample.__table__.indexes}
        assert "ix_metric_samples_scope_service_metric_ts" in idx
