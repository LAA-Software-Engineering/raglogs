import re
from datetime import datetime, timezone
from typing import Optional

from dateutil import parser as dateutil_parser

from src.utils.time import rewrite_iso_z

# Common timestamp patterns for plain-text log parsing
TIMESTAMP_PATTERNS = [
    # ISO 8601 with timezone
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})"),
    # ISO 8601 no timezone
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"),
    # Common log format: 2026-03-12 22:01:10
    re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?"),
    # Apache/nginx: 12/Mar/2026:22:01:10 +0000
    re.compile(r"\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}"),
    # Unix timestamp (10+ digit number)
    re.compile(r"\b1[5-9]\d{8}(?:\.\d+)?\b"),
]


def extract_timestamp(text: str) -> Optional[datetime]:
    """Try to extract a timestamp from arbitrary text."""
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(text)
        if match:
            raw = match.group(0)
            try:
                # Handle Unix timestamps
                if re.match(r"^1[5-9]\d{8}", raw):
                    return datetime.fromtimestamp(float(raw), tz=timezone.utc)
                dt = dateutil_parser.parse(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (ValueError, OverflowError):
                continue
    return None


def parse_timestamp_field(value: str | int | float) -> Optional[datetime]:
    """Parse a timestamp from a known field value."""
    if isinstance(value, (int, float)):
        try:
            # Handle millisecond timestamps
            if value > 1e12:
                value = value / 1000
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    if isinstance(value, str):
        # Fast path: ISO 8601 via the stdlib C parser. Log timestamps are
        # overwhelmingly ISO, and dateutil's generic parser is ~50x slower —
        # it dominated the ingest hot path (#85 profile). For the ISO strings
        # this codebase realistically sees, fromisoformat and dateutil agree, so
        # this reorders which parser wins; anything fromisoformat rejects falls
        # through unchanged. One known divergence: a UTC offset carrying a
        # seconds component (e.g. "+02:00:30") — fromisoformat keeps full
        # precision, where the old dateutil→regex path truncated to "+02:00".
        # More correct, and no real log source emits sub-minute offsets.
        try:
            dt = datetime.fromisoformat(rewrite_iso_z(value))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        try:
            dt = dateutil_parser.parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, OverflowError):
            # dateutil already handles bare-digit calendar strings correctly
            # ("20260312", "2026", a bare day-of-month) - only reached here
            # for a value it rejected outright, e.g. a Unix epoch whose
            # magnitude doesn't fit a year/day (a 13-digit ms epoch overflows,
            # a 10-digit epoch reads as an out-of-range year). Route only
            # that failure case through the (millisecond-aware) numeric
            # branch instead of preempting dateutil for every digit string.
            stripped = value.strip()
            if re.fullmatch(r"-?\d+(?:\.\d+)?", stripped):
                return parse_timestamp_field(float(stripped))
            return extract_timestamp(value)

    return None
