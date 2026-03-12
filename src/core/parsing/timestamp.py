import re
from datetime import datetime, timezone
from typing import Optional

from dateutil import parser as dateutil_parser

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
        try:
            dt = dateutil_parser.parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, OverflowError):
            return extract_timestamp(value)

    return None
