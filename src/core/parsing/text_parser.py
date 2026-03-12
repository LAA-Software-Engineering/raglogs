import re
from datetime import datetime
from typing import Optional

from src.core.parsing.field_extractors import normalize_level
from src.core.parsing.json_parser import ParsedLogLine
from src.core.parsing.timestamp import extract_timestamp

# Severity keywords
LEVEL_PATTERN = re.compile(
    r"\b(FATAL|CRITICAL|ERROR|ERR|WARN(?:ING)?|INFO(?:RMATION(?:AL)?)?|DEBUG|TRACE|VERBOSE)\b",
    re.IGNORECASE,
)

# Common plain-text log patterns
# Pattern: <timestamp> <LEVEL> <service> <message>
STRUCTURED_TEXT_PATTERN = re.compile(
    r"^(?P<ts>\S+(?:\s\S+)?)\s+(?P<level>FATAL|CRITICAL|ERROR|ERR|WARN(?:ING)?|INFO(?:RMATION(?:AL)?)?|DEBUG|TRACE|VERBOSE)\s+(?P<service>\S+)\s+(?P<message>.+)$",
    re.IGNORECASE,
)

# Pattern: [timestamp] [LEVEL] message
BRACKET_PATTERN = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s*\[?(?P<level>FATAL|CRITICAL|ERROR|ERR|WARN(?:ING)?|INFO|DEBUG|TRACE)\]?\s*(?P<message>.+)$",
    re.IGNORECASE,
)


def parse_text_line(
    line: str,
    default_service: Optional[str] = None,
) -> Optional[ParsedLogLine]:
    """Parse a single plain-text log line."""
    line_stripped = line.strip()
    if not line_stripped:
        return None

    timestamp: Optional[datetime] = None
    level: Optional[str] = None
    service: Optional[str] = None
    message: Optional[str] = None

    # Try structured pattern first
    m = STRUCTURED_TEXT_PATTERN.match(line_stripped)
    if m:
        timestamp = extract_timestamp(m.group("ts"))
        level = normalize_level(m.group("level"))
        service = m.group("service")
        message = m.group("message").strip()
    else:
        m2 = BRACKET_PATTERN.match(line_stripped)
        if m2:
            timestamp = extract_timestamp(m2.group("ts"))
            level = normalize_level(m2.group("level"))
            message = m2.group("message").strip()
        else:
            # Fallback: try to extract timestamp and level anywhere
            timestamp = extract_timestamp(line_stripped)

            level_match = LEVEL_PATTERN.search(line_stripped)
            if level_match:
                level = normalize_level(level_match.group(0))

            # Remove timestamp from message if found
            message = line_stripped
            if timestamp:
                # Strip the timestamp from front if possible
                ts_pattern = re.compile(
                    r"^\S+(?:\s\S+)?\s*" if " " in line_stripped[:30] else r"^\S+\s*"
                )
                message = ts_pattern.sub("", line_stripped).strip()
            if not message:
                message = line_stripped

    return ParsedLogLine(
        raw_line=line_stripped,
        timestamp=timestamp,
        level=level,
        service=service or default_service,
        message=message or line_stripped,
        parser_type="text",
    )
