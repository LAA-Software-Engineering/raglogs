"""Outermost HTTP middleware: request id, JSON log context, traces, latency."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from structlog.contextvars import bind_contextvars, clear_contextvars

from src.observability.metrics import (
    classify_http_path,
    record_ingest_request_duration,
    record_query_request_duration,
)
from src.observability.tracing import (
    current_trace_ids,
    extract_parent_context,
    get_tracer,
)

log = structlog.get_logger()

_W3C_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


def resolve_request_id(request: Request) -> str:
    """Honor ``X-Request-Id`` / ``X-Request-ID``, else generate a UUID4."""
    incoming = request.headers.get("x-request-id")
    if incoming and incoming.strip():
        return incoming.strip()[:128]
    return str(uuid.uuid4())


def _w3c_trace_id_from_request_id(request_id: str) -> str:
    """Return a 32-char hex trace-id. Hash when the request id is not already hex."""
    stripped = request_id.replace("-", "").lower()
    if _W3C_TRACE_ID.fullmatch(stripped):
        return stripped
    return hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]


def _apply_trace_headers(response: Response, request_id: str) -> None:
    trace_id, span_id, sampled = current_trace_ids()
    if trace_id is not None and span_id is not None and _W3C_TRACE_ID.fullmatch(trace_id):
        flags = "01" if sampled else "00"
        response.headers["traceparent"] = f"00-{trace_id}-{span_id}-{flags}"
        response.headers["X-Trace-Id"] = trace_id
        return
    # No valid span: still correlate via X-Trace-Id. Only emit traceparent when
    # we have a legal 32-hex id (UUID request ids, or a sha256 fallback).
    fallback_id = _w3c_trace_id_from_request_id(request_id)
    response.headers["X-Trace-Id"] = fallback_id
    response.headers["traceparent"] = f"00-{fallback_id}-{'0' * 16}-00"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Bind request_id (and later scope), start a root span, record latency.

    Outermost so 401/403 still echo ``X-Request-Id`` and trace headers.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = resolve_request_id(request)
        request.state.request_id = request_id
        bind_contextvars(request_id=request_id)

        kind, query_endpoint = classify_http_path(request.method, request.url.path)
        tracer = get_tracer()
        parent = extract_parent_context(request.headers)
        started = time.perf_counter()
        response: Response | None = None
        try:
            with tracer.start_as_current_span(
                "http.request",
                context=parent,
            ) as span:
                span.set_attribute("http.method", request.method)
                span.set_attribute("http.route", request.url.path)
                span.set_attribute("raglogs.request_id", request_id)
                try:
                    response = await call_next(request)
                except Exception:
                    elapsed = time.perf_counter() - started
                    _record_http_duration(kind, query_endpoint, elapsed)
                    log.exception(
                        "http_request_failed",
                        method=request.method,
                        path=request.url.path,
                    )
                    raise
                elapsed = time.perf_counter() - started
                _record_http_duration(kind, query_endpoint, elapsed)
                span.set_attribute("http.status_code", response.status_code)
                scope = getattr(request.state, "resolved_scope", None) or getattr(
                    request.state, "auth_scope", None
                )
                if scope:
                    bind_contextvars(scope=str(scope))
                    span.set_attribute("raglogs.scope", str(scope))
                log.info(
                    "http_request",
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    duration_ms=round(elapsed * 1000, 1),
                )
                response.headers["X-Request-Id"] = request_id
                _apply_trace_headers(response, request_id)
                return response
        finally:
            clear_contextvars()


def _record_http_duration(
    kind: str, query_endpoint: str | None, elapsed: float
) -> None:
    if kind == "query" and query_endpoint is not None:
        record_query_request_duration(query_endpoint, elapsed)
    elif kind == "ingest":
        record_ingest_request_duration(elapsed)
