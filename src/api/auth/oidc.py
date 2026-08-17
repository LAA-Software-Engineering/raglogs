"""Optional OIDC JWT validation via JWKS. No live IdP in unit tests — mock this."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from src.api.auth.keys import VALID_ROLES

ALLOWED_ALGORITHMS: tuple[str, ...] = (
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
)

_jwks_cache: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class OidcPrincipal:
    role: str
    scope: str
    subject: str | None
    auth_method: str = "oidc"


def looks_like_jwt(token: str) -> bool:
    """True when the bearer has three dotted segments (typical JWT)."""
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def clear_jwks_cache() -> None:
    _jwks_cache.clear()


def role_from_claims(claims: dict[str, Any]) -> str:
    """Map `raglogs_role` or `roles` to ingest|query|admin; default `query`."""
    role = claims.get("raglogs_role")
    if isinstance(role, str) and role in VALID_ROLES:
        return role
    roles = claims.get("roles")
    if isinstance(roles, str) and roles in VALID_ROLES:
        return roles
    if isinstance(roles, list):
        for item in roles:
            if item in VALID_ROLES:
                return str(item)
    return "query"


def _http_get_json(url: str) -> dict[str, Any]:
    with httpx.Client(timeout=10.0) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("OIDC response was not a JSON object")
    return payload


def _looks_like_jwks_url(url: str) -> bool:
    lowered = url.lower()
    return lowered.endswith("jwks.json") or lowered.endswith("/jwks") or "/jwks" in lowered


def resolve_jwks_uri(settings: Any) -> str:
    """JWKS URL: explicit setting, else OIDC discovery, else `{issuer}/.well-known/jwks.json`."""
    explicit = (getattr(settings, "oidc_jwks_url", "") or "").strip()
    if explicit:
        if _looks_like_jwks_url(explicit):
            return explicit
        try:
            document = _http_get_json(explicit)
            jwks_uri = document.get("jwks_uri")
            if isinstance(jwks_uri, str) and jwks_uri:
                return jwks_uri
        except (httpx.HTTPError, ValueError):
            return explicit
        return explicit

    issuer = (getattr(settings, "oidc_issuer", "") or "").rstrip("/")
    if not issuer:
        return ""
    discovery = f"{issuer}/.well-known/openid-configuration"
    try:
        document = _http_get_json(discovery)
        jwks_uri = document.get("jwks_uri")
        if isinstance(jwks_uri, str) and jwks_uri:
            return jwks_uri
    except (httpx.HTTPError, ValueError):
        pass
    return f"{issuer}/.well-known/jwks.json"


def get_jwks(settings: Any) -> dict[str, Any]:
    """Fetch JWKS, cached in process memory by URL."""
    uri = resolve_jwks_uri(settings)
    if not uri:
        raise ValueError("OIDC JWKS URL is not configured")
    cached = _jwks_cache.get(uri)
    if cached is not None:
        return cached
    document = _http_get_json(uri)
    _jwks_cache[uri] = document
    return document


def _key_from_jwks(jwks: dict[str, Any], kid: str | None) -> Any:
    keys = jwks.get("keys")
    if not isinstance(keys, list) or not keys:
        raise ValueError("JWKS document has no keys")
    selected: dict[str, Any] | None = None
    if kid:
        for item in keys:
            if isinstance(item, dict) and item.get("kid") == kid:
                selected = item
                break
    elif len(keys) == 1 and isinstance(keys[0], dict):
        selected = keys[0]
    if selected is None:
        raise ValueError("No matching JWK for token kid")
    return PyJWK.from_dict(selected).key


def validate_oidc_token(token: str, settings: Any) -> OidcPrincipal | None:
    """Validate signature, iss, exp, and optional aud. Returns None on failure."""
    issuer = (getattr(settings, "oidc_issuer", "") or "").strip()
    if not issuer:
        return None
    try:
        header = jwt.get_unverified_header(token)
        jwks = get_jwks(settings)
        key = _key_from_jwks(jwks, header.get("kid"))
        audience = (getattr(settings, "oidc_audience", "") or "").strip()
        decode_kwargs: dict[str, Any] = {
            "algorithms": list(ALLOWED_ALGORITHMS),
            "issuer": issuer,
            "options": {
                "verify_aud": bool(audience),
                "require": ["exp", "iss"],
            },
        }
        if audience:
            decode_kwargs["audience"] = audience
        claims = jwt.decode(token, key, **decode_kwargs)
    except (jwt.InvalidTokenError, ValueError, httpx.HTTPError, KeyError, TypeError):
        return None
    if not isinstance(claims, dict):
        return None
    return OidcPrincipal(
        role=role_from_claims(claims),
        scope="default",
        subject=str(claims["sub"]) if claims.get("sub") is not None else None,
    )
