from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import orjson

from src.core.parsing.field_extractors import extract_all_fields
from src.core.parsing.timestamp import parse_timestamp_field


@dataclass
class ParsedLogLine:
    raw_line: str
    timestamp: Optional[datetime] = None
    level: Optional[str] = None
    service: Optional[str] = None
    environment: Optional[str] = None
    trace_id: Optional[str] = None
    request_id: Optional[str] = None
    host: Optional[str] = None
    message: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    parser_type: str = "unknown"
    parse_error: Optional[str] = None


def parse_json_line(line: str, default_service: Optional[str] = None) -> Optional[ParsedLogLine]:
    """Parse a single JSON log line. Returns None on failure."""
    line = line.strip()
    if not line:
        return None

    try:
        data = orjson.loads(line)
    except Exception as e:
        return ParsedLogLine(
            raw_line=line,
            parser_type="json",
            parse_error=f"JSON parse error: {e}",
        )

    if not isinstance(data, dict):
        return ParsedLogLine(
            raw_line=line,
            parser_type="json",
            parse_error="JSON line is not an object",
        )

    fields = extract_all_fields(data)

    timestamp = None
    if fields["timestamp_raw"] is not None:
        timestamp = parse_timestamp_field(fields["timestamp_raw"])

    service = fields["service"] or default_service
    if service is not None:
        service = str(service)

    message = fields["message"]
    if message is not None:
        message = str(message)
    else:
        # fallback: dump the whole dict as message
        message = line

    return ParsedLogLine(
        raw_line=line,
        timestamp=timestamp,
        level=fields["level"],
        service=service,
        environment=str(fields["environment"]) if fields["environment"] else None,
        trace_id=str(fields["trace_id"]) if fields["trace_id"] else None,
        request_id=str(fields["request_id"]) if fields["request_id"] else None,
        host=str(fields["host"]) if fields["host"] else None,
        message=message,
        extra=fields["extra"] or {},
        parser_type="json",
    )
