"""Mark unversioned ingest/query/config aliases as deprecated.

Canonical routes live under ``/v1/``. Unversioned ``/ingestions``, ``/query``,
and ``/config`` stay mounted for one release and send ``Deprecation: true``
plus a ``Link`` successor-version header. Health, the web UI, and ``/static``
are not deprecated.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_DEPRECATED_ROOTS: tuple[str, ...] = ("/ingestions", "/query", "/config")
_API_VERSION_PREFIX = re.compile(r"^/v\d+(?=/|$)")


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path


def is_versioned_api_path(path: str) -> bool:
    """True for ``/v1``, ``/v2``, … and anything under those prefixes."""
    return _API_VERSION_PREFIX.match(_normalize_path(path)) is not None


def is_deprecated_alias(path: str) -> bool:
    """True for unversioned ingest/query/config paths (not ``/v1/...``)."""
    normalized = _normalize_path(path)
    if is_versioned_api_path(normalized):
        return False
    for root in _DEPRECATED_ROOTS:
        if normalized == root or normalized.startswith(root + "/"):
            return True
    return False


def successor_path(path: str) -> str:
    """Map an unversioned alias onto its canonical ``/v1`` path."""
    normalized = _normalize_path(path)
    if is_versioned_api_path(normalized):
        return normalized
    return "/v1" + normalized


class DeprecationHeaderMiddleware(BaseHTTPMiddleware):
    """Attach RFC 8594 deprecation headers on unversioned API aliases."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        if is_deprecated_alias(path):
            response.headers["Deprecation"] = "true"
            response.headers["Link"] = f'<{successor_path(path)}>; rel="successor-version"'
        return response
