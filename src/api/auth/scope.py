"""Resolve the isolation scope for an HTTP request (G8).

Service requests always end with a non-empty scope, or ``400 SCOPE_REQUIRED``.
Pinned API keys (and OIDC principals) cannot switch to another scope.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Request
from starlette.responses import JSONResponse

from structlog.contextvars import bind_contextvars

from src.api.auth.middleware import AuthPrincipal
from src.db.models import DEFAULT_LOG_SCOPE

ERROR_SCOPE_REQUIRED = "SCOPE_REQUIRED"
ERROR_SCOPE_MISMATCH = "SCOPE_MISMATCH"


class ScopeResolutionError(Exception):
    """Raised when a service request has no resolvable scope, or a pinned mismatch."""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(message)


def scope_error_response(exc: ScopeResolutionError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error_code": exc.error_code, "message": exc.message},
    )


def normalize_requested_scope(value: Optional[str]) -> Optional[str]:
    """Strip whitespace. Empty / None means "not provided"."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def resolve_scope(
    *,
    requested_scope: Optional[str],
    auth_enabled: bool,
    principal: Optional[AuthPrincipal],
) -> str:
    """Return the scope to stamp and filter with.

    Auth off: explicit request scope, else ``default`` (unauthenticated tests).
    Pinned key / OIDC: always the principal scope; a different request scope
    is ``403 SCOPE_MISMATCH``. Override-allowed keys use the request scope
    when present, else the key scope. Empty after resolution → ``400``.
    """
    requested = normalize_requested_scope(requested_scope)

    if not auth_enabled:
        resolved = requested or DEFAULT_LOG_SCOPE
        if not resolved:
            raise ScopeResolutionError(
                400,
                ERROR_SCOPE_REQUIRED,
                "A non-empty scope is required",
            )
        return resolved

    if principal is None:
        raise ScopeResolutionError(
            400,
            ERROR_SCOPE_REQUIRED,
            "A resolvable scope is required for this request",
        )

    pinned = bool(
        principal.auth_method == "oidc" or not principal.allow_scope_override
    )
    principal_scope = normalize_requested_scope(principal.scope)

    if pinned:
        if not principal_scope:
            raise ScopeResolutionError(
                400,
                ERROR_SCOPE_REQUIRED,
                "API key has no scope; mint a key with --scope",
            )
        if requested is not None and requested != principal_scope:
            raise ScopeResolutionError(
                403,
                ERROR_SCOPE_MISMATCH,
                "Request scope does not match the API key's pinned scope",
            )
        return principal_scope

    resolved = requested or principal_scope
    if not resolved:
        raise ScopeResolutionError(
            400,
            ERROR_SCOPE_REQUIRED,
            "Pass scope on the request or mint a key with a default scope",
        )
    return resolved


def requested_scope_from_http(
    request: Request,
    body_scope: Optional[str] = None,
) -> Optional[str]:
    """Prefer a JSON-body ``scope``; otherwise the ``scope`` query parameter."""
    from_body = normalize_requested_scope(body_scope)
    if from_body is not None:
        return from_body
    return normalize_requested_scope(request.query_params.get("scope"))


def bind_request_scope(
    request: Request,
    body_scope: Optional[str] = None,
) -> str:
    """Resolve scope, store it on ``request.state``, and return it."""
    from src.config import get_settings

    settings = get_settings()
    principal = getattr(request.state, "auth_principal", None)
    if principal is not None and not isinstance(principal, AuthPrincipal):
        principal = None

    resolved = resolve_scope(
        requested_scope=requested_scope_from_http(request, body_scope),
        auth_enabled=bool(settings.auth_enabled),
        principal=principal,
    )
    request.state.resolved_scope = resolved
    bind_contextvars(scope=resolved)
    return resolved
