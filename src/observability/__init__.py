"""Process-wide logging, Prometheus metrics, and OpenTelemetry tracing (G12)."""

from __future__ import annotations

from typing import Any

from src.observability.logging import configure_logging
from src.observability.tracing import setup_tracing


def setup_observability(
    *,
    log_format: str | None = None,
    extra_processors: list[Any] | None = None,
) -> None:
    """Configure structured logging and tracing once per process."""
    configure_logging(log_format=log_format, extra_processors=extra_processors)
    setup_tracing()
