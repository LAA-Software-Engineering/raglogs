import hashlib

from src.core.normalization.normalize import normalize_message


def compute_fingerprint(normalized_message: str) -> str:
    """Compute a stable SHA-256 fingerprint (truncated to 16 chars) for a normalized message."""
    if not normalized_message:
        return "empty"
    digest = hashlib.sha256(normalized_message.encode("utf-8")).hexdigest()
    return digest[:16]


def fingerprint_message(raw_message: str) -> tuple[str, str]:
    """
    Given a raw message, return (normalized_message, fingerprint).
    """
    normalized = normalize_message(raw_message)
    fp = compute_fingerprint(normalized)
    return normalized, fp
