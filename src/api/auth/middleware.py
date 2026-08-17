"""ASGI/HTTP middleware: Bearer API keys or optional OIDC JWT.

Settings are read per request so tests can patch `src.config.get_settings`
on the existing TestClient. Bearer tokens are never written to logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

ERROR_UNAUTHORIZED = "AUTH_UNAUTHORIZED"
ERROR_FORBIDDEN = "AUTH_FORBIDDEN"


@dataclass(frozen=True)
class AuthPrincipal:
    role: str
    scope: str
    auth_method: str
    key_id: str | None = None
    subject: str | None = None


def _auth_error(status_code: int, error_code: str, message: str) -> JSONResponse:
    headers = {}
    if status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=status_code,
        content={"error_code": error_code, "message": message},
        headers=headers,
    )


def _extract_bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, remainder = header.partition(" ")
    if scheme.lower() != "bearer" or not remainder.strip():
        return None
    return remainder.strip()


def _authenticate_token(token: str, settings: Any) -> AuthPrincipal | None:
    from src.api.auth.oidc import looks_like_jwt, validate_oidc_token

    mode = getattr(settings, "auth_mode", "api_key")
    if looks_like_jwt(token):
        if mode not in ("oidc", "both"):
            return None
        principal = validate_oidc_token(token, settings)
        if principal is None:
            return None
        return AuthPrincipal(
            role=principal.role,
            scope=principal.scope,
            auth_method="oidc",
            subject=principal.subject,
        )

    if mode not in ("api_key", "both"):
        return None
    from src.api.auth.keys import lookup_api_key

    record = lookup_api_key(token)
    if record is None:
        return None
    return AuthPrincipal(
        role=record.role,
        scope=record.scope,
        auth_method="api_key",
        key_id=str(record.id),
    )


def authorize_request(request: Request) -> JSONResponse | None:
    """Return an error response, or None if the request may proceed.

    Reads settings on every call (not at import) so AUTH_ENABLED can be patched.
    """
    from src.api.auth.roles import required_roles
    from src.config import get_settings

    allowed = required_roles(request.method, request.url.path)
    if allowed is None:
        return None

    settings = get_settings()
    if not settings.auth_enabled:
        return None

    token = _extract_bearer(request)
    if token is None:
        return _auth_error(
            401,
            ERROR_UNAUTHORIZED,
            "Missing or invalid Authorization bearer token",
        )

    principal = _authenticate_token(token, settings)
    if principal is None:
        return _auth_error(
            401,
            ERROR_UNAUTHORIZED,
            "Invalid or revoked credentials",
        )

    if principal.role not in allowed:
        return _auth_error(
            403,
            ERROR_FORBIDDEN,
            "Insufficient role for this endpoint",
        )

    request.state.auth_principal = principal
    request.state.auth_role = principal.role
    request.state.auth_scope = principal.scope
    return None


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        error = authorize_request(request)
        if error is not None:
            return error
        return await call_next(request)
