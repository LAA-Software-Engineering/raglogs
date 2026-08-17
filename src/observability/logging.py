"""Structlog configuration — JSON by default, console renderer optional."""

from __future__ import annotations

import logging
from typing import Any

import structlog

_configured: bool = False

_REDACT_KEYS: frozenset[str] = frozenset(
    {
        "authorization",
        "api_key",
        "openai_api_key",
        "anthropic_api_key",
        "password",
        "token",
        "bearer",
        "webhook_secret",
        "secret",
        "datadog_api_key",
        "datadog_app_key",
        "loki_password",
        "loki_bearer_token",
    }
)


def _redact_secrets(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Drop raw secrets if a caller accidentally logs them."""
    for key in list(event_dict):
        lowered = key.lower()
        if lowered in _REDACT_KEYS or lowered.endswith("_secret") or lowered.endswith("_token"):
            if lowered in {"request_id", "trace_id"}:
                continue
            event_dict[key] = "***"
    return event_dict


def configure_logging(
    log_format: str | None = None,
    extra_processors: list[Any] | None = None,
) -> None:
    """Configure structlog once. Safe to call from API and CLI.

    ``extra_processors`` run after context merge and before the renderer —
    used by tests to capture event dicts.
    """
    global _configured
    from src.config import get_settings

    fmt = log_format or get_settings().log_format
    if fmt not in ("json", "console"):
        fmt = "json"

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_secrets,
    ]
    if extra_processors:
        processors.extend(extra_processors)
    if fmt == "console":
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    _configured = True
