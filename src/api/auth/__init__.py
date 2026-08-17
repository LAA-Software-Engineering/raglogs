"""HTTP API authentication (API keys, optional OIDC, bind-host guard)."""

from src.api.auth.bind_guard import InsecureBindError, is_loopback_host, warn_if_insecure_bind
from src.api.auth.keys import (
    ApiKeyInfo,
    generate_api_key,
    hash_api_key,
    key_prefix,
    lookup_api_key,
    verify_api_key,
)
from src.api.auth.middleware import AuthMiddleware, authorize_request
from src.api.auth.roles import required_roles

__all__ = [
    "ApiKeyInfo",
    "AuthMiddleware",
    "InsecureBindError",
    "authorize_request",
    "generate_api_key",
    "hash_api_key",
    "is_loopback_host",
    "key_prefix",
    "lookup_api_key",
    "required_roles",
    "verify_api_key",
    "warn_if_insecure_bind",
]
