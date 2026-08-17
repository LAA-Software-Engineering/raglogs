"""Warn (or refuse) when the API binds off-loopback with authentication disabled."""

from __future__ import annotations

from typing import Any

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


class InsecureBindError(RuntimeError):
    """Raised when AUTH_REFUSE_INSECURE_BIND is set and the bind is not loopback."""


def is_loopback_host(host: str) -> bool:
    """True for 127.0.0.1, ::1, localhost, and other 127.0.0.0/8 addresses."""
    raw = (host or "").strip().lower()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    if raw in LOOPBACK_HOSTS:
        return True
    if raw.startswith("127."):
        return True
    return False


def warn_if_insecure_bind(host: str, settings: Any) -> None:
    """Log a loud warning when auth is off and `host` is not loopback.

    If `settings.auth_refuse_insecure_bind` is True, raise InsecureBindError
    instead of (or after) warning so the process refuses to start.
    """
    if getattr(settings, "auth_enabled", False):
        return
    if is_loopback_host(host):
        return

    import structlog

    log = structlog.get_logger()
    log.warning(
        "auth_disabled_on_non_loopback_bind",
        host=host,
        hint="Set AUTH_ENABLED=true before exposing the API on a network interface",
    )
    if getattr(settings, "auth_refuse_insecure_bind", False):
        raise InsecureBindError(
            f"Refusing to bind {host!r} with AUTH_ENABLED=false "
            "(set AUTH_ENABLED=true or AUTH_REFUSE_INSECURE_BIND=false)"
        )
