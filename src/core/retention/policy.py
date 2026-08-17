"""Retention interval parsing, cutoffs, and per-scope overrides."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.config.settings import Settings
from src.utils.time import parse_duration

# 0 / empty / off (and a few aliases) mean "never purge this tier".
NEVER_TOKENS = frozenset({"", "0", "off", "none", "never", "false"})


@dataclass(frozen=True)
class RetentionPolicy:
    scope: str
    raw_interval: str
    summary_interval: str


def is_retention_disabled(value: Optional[str]) -> bool:
    """True when the interval means never purge that tier."""
    if value is None:
        return True
    stripped = value.strip().lower()
    if stripped in NEVER_TOKENS:
        return True
    try:
        return parse_duration(stripped).total_seconds() <= 0
    except ValueError:
        return False


def parse_retention_interval(value: Optional[str]) -> Optional[timedelta]:
    """Parse a retention string into a timedelta, or None to skip the tier.

    ``None``, empty, ``0``, and ``off`` disable expiry. Invalid strings raise
    ``ValueError``. A parsed duration of zero or less also disables expiry.
    """
    if value is None:
        return None
    stripped = value.strip()
    if stripped.lower() in NEVER_TOKENS:
        return None
    if any(ch.isspace() for ch in stripped):
        raise ValueError(f"Cannot parse retention interval: {value!r}")
    delta = parse_duration(stripped)
    if delta.total_seconds() <= 0:
        return None
    return delta


def compute_cutoff(
    interval: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Return the expiry cutoff, or None when that tier should not be purged."""
    delta = parse_retention_interval(interval)
    if delta is None:
        return None
    clock = now or datetime.now(tz=timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock - delta


def validate_policy_intervals(policy: RetentionPolicy) -> None:
    """Raise ``ValueError`` if either interval string cannot be parsed."""
    parse_retention_interval(policy.raw_interval)
    parse_retention_interval(policy.summary_interval)


def apply_scope_override(
    *,
    scope: str,
    default_raw: str,
    default_summary: str,
    override_raw: Optional[str] = None,
    override_summary: Optional[str] = None,
) -> RetentionPolicy:
    """Merge a ``scope_retention`` row with env defaults.

    ``None`` on an override column means "missing" → env default. An empty
    string is a present override and disables that tier.
    """
    raw = default_raw if override_raw is None else override_raw
    summary = default_summary if override_summary is None else override_summary
    return RetentionPolicy(scope=scope, raw_interval=raw, summary_interval=summary)


def resolve_scope_policy(
    db: Any,
    scope: str,
    settings: Optional[Settings] = None,
) -> RetentionPolicy:
    """Load per-scope overrides; fall back to ``RETENTION_RAW`` / ``RETENTION_SUMMARY``."""
    from src.config import get_settings
    from src.db.models import ScopeRetention

    cfg = settings or get_settings()
    row = db.get(ScopeRetention, scope)
    override_raw = getattr(row, "raw_interval", None) if row is not None else None
    override_summary = (
        getattr(row, "summary_interval", None) if row is not None else None
    )
    return apply_scope_override(
        scope=scope,
        default_raw=cfg.retention_raw,
        default_summary=cfg.retention_summary,
        override_raw=override_raw,
        override_summary=override_summary,
    )
