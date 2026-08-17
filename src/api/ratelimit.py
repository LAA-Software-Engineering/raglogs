"""Per-API-key token-bucket rate limiting for ingest and query routes (G9).

Buckets are in-memory and per process — they are not shared across
uvicorn workers or hosts. Identity is ``request.state.auth_principal.key_id``
when present; otherwise ``\"anonymous\"`` (including AUTH_ENABLED=false).

``RATELIMIT_INGEST_RPS`` / ``RATELIMIT_QUERY_RPS`` of 0 means unlimited for
that category. ``RATELIMIT_ENABLED=false`` disables limiting entirely.
Health, docs, static UI, config, and key-admin routes are not limited.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Awaitable, Callable, Literal

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

ERROR_RATE_LIMITED = "RATE_LIMITED"

RateLimitKind = Literal["ingest", "query"]

_API_VERSION_PREFIX = re.compile(r"^/v\d+(?=/|$)")

_store_lock = threading.Lock()
_buckets: dict[tuple[str, str], "_TokenBucket"] = {}


class _TokenBucket:
    """Thread-safe token bucket. Rate/burst changes reset the fill level."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: float = 0.0
        self._updated: float = time.monotonic()
        self._rate: float = 0.0
        self._burst: float = 0.0

    def consume(self, rate: float, burst: float, tokens: float = 1.0) -> bool:
        if rate <= 0:
            return True
        capacity = max(float(burst), tokens)
        with self._lock:
            now = time.monotonic()
            if rate != self._rate or capacity != self._burst:
                self._rate = rate
                self._burst = capacity
                self._tokens = capacity
                self._updated = now
            else:
                elapsed = now - self._updated
                self._tokens = min(capacity, self._tokens + elapsed * rate)
                self._updated = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


def reset_rate_limiter() -> None:
    """Drop all buckets. Used by tests so cases do not leak fill state."""
    with _store_lock:
        _buckets.clear()


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path


def rate_limit_kind(method: str, path: str) -> RateLimitKind | None:
    """Return ingest/query for limited routes, else None (exempt)."""
    normalized = _API_VERSION_PREFIX.sub("", _normalize_path(path), count=1)
    if not normalized:
        normalized = "/"
    verb = method.upper()
    if normalized == "/ingestions" or normalized.startswith("/ingestions/"):
        if verb == "POST":
            return "ingest"
        return None
    if normalized == "/query" or normalized.startswith("/query/"):
        return "query"
    return None


def bucket_identity(request: Request) -> str:
    """Per-key identity, or anonymous when auth is off / principal missing."""
    principal = getattr(request.state, "auth_principal", None)
    if principal is None:
        return "anonymous"
    key_id = getattr(principal, "key_id", None)
    if key_id:
        return str(key_id)
    subject = getattr(principal, "subject", None)
    if subject:
        return f"oidc:{subject}"
    return "anonymous"


def allow_request(kind: RateLimitKind, identity: str, rate: float, burst: float) -> bool:
    """Consume one token from the (kind, identity) bucket. True if allowed."""
    if rate <= 0:
        return True
    key = (kind, identity)
    with _store_lock:
        bucket = _buckets.get(key)
        if bucket is None:
            bucket = _TokenBucket()
            _buckets[key] = bucket
    return bucket.consume(rate, burst)


def _limited_response(retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "error_code": ERROR_RATE_LIMITED,
            "message": "Rate limit exceeded; retry after the Retry-After delay.",
        },
        headers={"Retry-After": str(int(retry_after))},
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket limiter applied after auth so key_id is available."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        from src.config import get_settings

        settings = get_settings()
        if not settings.ratelimit_enabled:
            return await call_next(request)

        kind = rate_limit_kind(request.method, request.url.path)
        if kind is None:
            return await call_next(request)

        rate = (
            settings.ratelimit_ingest_rps
            if kind == "ingest"
            else settings.ratelimit_query_rps
        )
        if rate <= 0:
            return await call_next(request)

        identity = bucket_identity(request)
        if not allow_request(kind, identity, rate, settings.ratelimit_burst):
            return _limited_response(settings.ratelimit_retry_after_seconds)
        return await call_next(request)
