"""NDJSON parsing for POST /ingestions/lines.

Each non-empty line is either a raw log string or a JSON value:
- JSON object with ``message`` / ``raw`` / ``text`` (plus optional timestamp,
  service, level, host, env) → serialized as a JSON log line
- JSON string → the unquoted string is the raw line
- anything else → treated as a raw text log line
"""

from __future__ import annotations

from typing import Any

import orjson


class NdjsonParseError(ValueError):
    """Invalid or oversized NDJSON payload."""


def parse_ndjson_payload(body: str, max_lines: int) -> list[str]:
    """Split an NDJSON body into raw lines ready for ``_process_line``.

    Raises ``NdjsonParseError`` when the payload is empty or exceeds ``max_lines``.
    Blank lines are ignored and do not count toward the cap.
    """
    if max_lines < 1:
        raise NdjsonParseError("max_lines must be >= 1")

    physical: list[str] = [line for line in body.splitlines() if line.strip()]
    if not physical:
        raise NdjsonParseError("NDJSON body is empty")
    if len(physical) > max_lines:
        raise NdjsonParseError(
            f"payload exceeds INGEST_PUSH_MAX_LINES ({max_lines})"
        )

    return [_coerce_ndjson_line(line) for line in physical]


def _coerce_ndjson_line(line: str) -> str:
    stripped = line.strip()
    try:
        parsed: Any = orjson.loads(stripped)
    except orjson.JSONDecodeError:
        return stripped

    if isinstance(parsed, str):
        return parsed

    if isinstance(parsed, dict):
        return _object_to_json_line(parsed)

    # Arrays / numbers / bools / null — keep the original text so the
    # existing parsers can reject or stringify them.
    return stripped


def _object_to_json_line(obj: dict[str, Any]) -> str:
    """Ensure a JSON object has a ``message`` field the json parser can use."""
    if "message" not in obj:
        for key in ("raw", "text", "msg"):
            if key in obj and obj[key] is not None:
                obj = {**obj, "message": obj[key]}
                break
    return orjson.dumps(obj).decode()
