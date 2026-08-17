"""OpenTelemetry tracing. Default exporter is none so tests/demo need no collector."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_initialized: bool = False
_propagator = TraceContextTextMapPropagator()


def setup_tracing() -> None:
    """Install a TracerProvider. No exporter unless OTLP endpoint is set."""
    global _initialized
    if _initialized:
        return

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import ProxyTracerProvider

    from src.config import get_settings

    settings = get_settings()
    if settings.otel_sdk_disabled:
        _initialized = True
        return

    current = trace.get_tracer_provider()
    if not isinstance(current, ProxyTracerProvider):
        _initialized = True
        return

    resource = Resource.create({"service.name": settings.otel_service_name or "raglogs"})
    provider = TracerProvider(resource=resource)

    endpoint = (settings.otel_exporter_otlp_endpoint or "").strip()
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    _initialized = True


def get_tracer() -> Tracer:
    setup_tracing()
    return trace.get_tracer("raglogs")


@contextmanager
def start_span(name: str, **attributes: Any) -> Iterator[Span]:
    """Child span; no-op when the SDK is disabled."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is None:
                continue
            if isinstance(value, (bool, int, float, str)):
                span.set_attribute(key, value)
            else:
                span.set_attribute(key, str(value))
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise


def extract_parent_context(headers: Any) -> Any:
    """Read W3C ``traceparent`` from incoming request headers."""
    return _propagator.extract(carrier=headers)


def current_trace_ids() -> tuple[str | None, str | None, bool]:
    """Return ``(trace_id_hex, span_id_hex, sampled)`` for the current span."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx is None or not ctx.is_valid:
        return None, None, False
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x"), bool(ctx.trace_flags.sampled)
