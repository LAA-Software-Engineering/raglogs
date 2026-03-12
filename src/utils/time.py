import re
from datetime import datetime, timedelta, timezone
from typing import Optional


def parse_duration(duration_str: str) -> timedelta:
    """
    Parse a duration string like '30m', '1h', '24h', '7d' into a timedelta.
    """
    pattern = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|hr|d|day|days|w|week|weeks)$", re.IGNORECASE)
    m = pattern.match(duration_str.strip())
    if not m:
        raise ValueError(f"Cannot parse duration: {duration_str!r}")

    value = float(m.group(1))
    unit = m.group(2).lower()

    if unit in ("s", "sec"):
        return timedelta(seconds=value)
    elif unit in ("m", "min"):
        return timedelta(minutes=value)
    elif unit in ("h", "hr"):
        return timedelta(hours=value)
    elif unit in ("d", "day", "days"):
        return timedelta(days=value)
    elif unit in ("w", "week", "weeks"):
        return timedelta(weeks=value)

    raise ValueError(f"Unknown unit: {unit}")


def resolve_window(
    since: Optional[str] = None,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    """
    Resolve a time window from CLI arguments.

    Returns (window_start, window_end) as timezone-aware datetimes.
    """
    now = datetime.now(tz=timezone.utc)

    if since:
        delta = parse_duration(since)
        return now - delta, now

    if from_time and to_time:
        if from_time.tzinfo is None:
            from_time = from_time.replace(tzinfo=timezone.utc)
        if to_time.tzinfo is None:
            to_time = to_time.replace(tzinfo=timezone.utc)
        return from_time, to_time

    if from_time:
        if from_time.tzinfo is None:
            from_time = from_time.replace(tzinfo=timezone.utc)
        return from_time, now

    raise ValueError("Must provide either --since or --from/--to")


def resolve_baseline_window(
    window_start: datetime,
    window_end: datetime,
    baseline_window_str: str = "24h",
) -> tuple[datetime, datetime]:
    """
    Resolve the baseline window that precedes the incident window.
    """
    window_duration = window_end - window_start
    baseline_duration = parse_duration(baseline_window_str)

    # Baseline ends just before the incident window
    baseline_end = window_start
    baseline_start = baseline_end - baseline_duration

    return baseline_start, baseline_end


def format_window(start: datetime, end: datetime) -> str:
    """Format a window as a human-readable string."""
    def fmt(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    return f"{fmt(start)} → {fmt(end)}"
