import re

from src.core.normalization.patterns import NORMALIZATION_RULES

# Collapse multiple whitespace
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_message(message: str) -> str:
    """
    Normalize a log message by replacing dynamic values with placeholders.
    Returns a stable normalized form suitable for fingerprinting.
    """
    if not message:
        return ""

    result = message

    for pattern, replacement in NORMALIZATION_RULES:
        result = pattern.sub(replacement, result)

    # Collapse multiple whitespace
    result = _WHITESPACE_RE.sub(" ", result).strip()

    return result
