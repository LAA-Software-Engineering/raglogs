import re

# Ordered list of (pattern, replacement) for message normalization
# Order matters: more specific patterns first
NORMALIZATION_RULES: list[tuple[re.Pattern, str]] = [
    # UUIDs
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    # JWT / long base64 tokens (30+ chars of base64url)
    (re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}"), "<jwt>"),
    # Numeric IDs after common keywords — must run BEFORE the hex rule,
    # otherwise an 8+ digit decimal ID (e.g. "user 12345678") is swallowed
    # by the hex rule and normalized to <hex>, fragmenting equivalent lines
    # ("user 12345678 not found" vs "user 4567 not found") across clusters.
    (re.compile(r"\b(user|account|order|transaction|session|job|task|worker|tenant|customer|invoice)\s+(?:id\s+)?#?(\d+)\b", re.IGNORECASE), r"\1 <id>"),
    # Hex strings 8+ chars (hashes, IDs)
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<hex>"),
    # Long alphanumeric tokens that look like API keys (20+ mixed chars)
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "<token>"),
    # IPv4 addresses
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    # IPv6 addresses
    (re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}\b"), "<ipv6>"),
    # Email addresses
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "<email>"),
    # URLs with query params - strip query
    (re.compile(r"(https?://[^\s?]+)\?[^\s]*"), r"\1?<params>"),
    # request_id=<value> style KV pairs with dynamic values
    (re.compile(r"\b(request_id|req_id|trace_id|span_id|correlation_id)=\S+"), r"\1=<*>"),
    # Duration values like 123ms, 1.5s, 200ms
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds?|minutes?|min|hours?|hr)\b"), "<duration>"),
    # Port numbers standalone (not part of IP above)
    (re.compile(r":(\d{4,5})\b"), ":<port>"),
    # Pure numeric values (but not short codes like 200, 404 etc that convey meaning)
    # Only replace long numbers (5+ digits) that are likely IDs
    (re.compile(r"\b\d{5,}\b"), "<num>"),
    # File paths with many components
    (re.compile(r"/(?:[a-zA-Z0-9_\-\.]+/){3,}[a-zA-Z0-9_\-\.]*"), "<path>"),
    # Timestamps within messages
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<timestamp>"),
]

# Patterns that indicate trigger events
TRIGGER_PATTERNS: list[re.Pattern] = [
    re.compile(r"deploy(?:ment)?\s+(?:started|completed|finished|done)", re.IGNORECASE),
    re.compile(r"application\s+(?:started|restarted|restart)", re.IGNORECASE),
    re.compile(r"service\s+(?:started|restarted|restart)", re.IGNORECASE),
    re.compile(r"pod\s+(?:restart|restarted|terminated|evicted)", re.IGNORECASE),
    re.compile(r"config(?:uration)?\s+(?:reloaded|changed|updated)", re.IGNORECASE),
    re.compile(r"migration\s+(?:started|completed|running)", re.IGNORECASE),
    re.compile(r"queue\s+(?:full|saturated|overflow)", re.IGNORECASE),
    re.compile(r"circuit[\s_]?breaker\s+(?:open|tripped|activated)", re.IGNORECASE),
    re.compile(r"webhook\s+(?:secret|key|config)\s+(?:changed|invalid|expired)", re.IGNORECASE),
    re.compile(r"token\s+(?:expired|expiration|invalid)", re.IGNORECASE),
    re.compile(r"release\s+\S+\s+(?:deployed|started|live)", re.IGNORECASE),
    re.compile(r"rollout\s+(?:started|completed|done)", re.IGNORECASE),
]


def is_trigger_message(message: str) -> bool:
    """Check if a log message matches a known trigger pattern."""
    for pattern in TRIGGER_PATTERNS:
        if pattern.search(message):
            return True
    return False


# Coarse type labels for the v1 JSON trigger object (G7). Order matters:
# more specific families first so "pod restarted" is pod, not restart.
_TRIGGER_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"deploy(?:ment)?|rollout|release\s+\S+\s+(?:deployed|started|live)", re.IGNORECASE), "deploy"),
    (re.compile(r"pod\s+(?:restart|restarted|terminated|evicted)", re.IGNORECASE), "pod"),
    (re.compile(r"config(?:uration)?\s+(?:reloaded|changed|updated)", re.IGNORECASE), "config"),
    (re.compile(r"migration\s+(?:started|completed|running)", re.IGNORECASE), "migration"),
    (re.compile(r"queue\s+(?:full|saturated|overflow)", re.IGNORECASE), "queue"),
    (re.compile(r"circuit[\s_]?breaker\s+(?:open|tripped|activated)", re.IGNORECASE), "circuit_breaker"),
    (re.compile(r"webhook\s+(?:secret|key|config)\s+(?:changed|invalid|expired)", re.IGNORECASE), "webhook"),
    (re.compile(r"token\s+(?:expired|expiration|invalid)", re.IGNORECASE), "token"),
    (re.compile(r"(?:application|service)\s+(?:started|restarted|restart)|restart", re.IGNORECASE), "restart"),
]


def infer_trigger_type(message: str) -> str | None:
    """Best-effort trigger family from a candidate message, or None."""
    if not message:
        return None
    for pattern, trigger_type in _TRIGGER_TYPE_PATTERNS:
        if pattern.search(message):
            return trigger_type
    return None
