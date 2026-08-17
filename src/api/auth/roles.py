"""Route → allowed role mapping.

`admin` is included in every non-exempt set. Scope is stored on keys for later
G8 isolation; this module does not filter log queries by scope.

A leading `/v1` or `/v2` (any `/v<digits>`) is stripped before matching, so
`POST /v1/ingestions` uses the same roles as `POST /ingestions`.

Exempt (no auth): GET/HEAD `/health`, `/metrics` (prefix match). `/docs` is
not exempt — OpenAPI can leak adapter-oriented config.

Role map
--------
POST /ingestions          ingest, admin
GET  /ingestions*         query, admin   (job listing/status is query-ish)
POST /query/*             query, admin
GET  /config              admin
GET  /                    query, admin   (web UI)
GET  /static*             query, admin
GET  /docs, /redoc, /openapi.json   query, admin
POST /admin/*             admin
unmatched paths           admin
"""

from __future__ import annotations

import re

INGEST_ROLES: frozenset[str] = frozenset({"ingest", "admin"})
QUERY_ROLES: frozenset[str] = frozenset({"query", "admin"})
ADMIN_ROLES: frozenset[str] = frozenset({"admin"})

EXEMPT_PREFIXES: tuple[str, ...] = ("/health", "/metrics")
_API_VERSION_PREFIX = re.compile(r"^/v\d+(?=/|$)")


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path


def _strip_api_version(path: str) -> str:
    """Map `/v1/query/explain` onto `/query/explain` for role matching."""
    stripped = _API_VERSION_PREFIX.sub("", path, count=1)
    return stripped if stripped else "/"


def is_exempt_path(path: str) -> bool:
    """True for `/health` and `/metrics` (and nested paths under those)."""
    normalized = _strip_api_version(_normalize_path(path))
    for prefix in EXEMPT_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def required_roles(method: str, path: str) -> frozenset[str] | None:
    """Return allowed roles for this request, or None if the path is exempt."""
    if is_exempt_path(path):
        return None

    normalized = _strip_api_version(_normalize_path(path))
    verb = method.upper()

    if normalized == "/ingestions" or normalized.startswith("/ingestions/"):
        if verb == "POST":
            return INGEST_ROLES
        return QUERY_ROLES

    if normalized == "/query" or normalized.startswith("/query/"):
        return QUERY_ROLES

    if normalized == "/config" or normalized.startswith("/config/"):
        return ADMIN_ROLES

    if normalized == "/" or normalized.startswith("/static"):
        return QUERY_ROLES

    if (
        normalized in {"/docs", "/redoc", "/openapi.json"}
        or normalized.startswith("/docs/")
        or normalized.startswith("/redoc/")
    ):
        return QUERY_ROLES

    if normalized == "/admin" or normalized.startswith("/admin/"):
        return ADMIN_ROLES

    return ADMIN_ROLES
