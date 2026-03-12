import hashlib
import json
from typing import Any


def hash_dict(data: dict[str, Any]) -> str:
    """Stable hash of a dict (sorted keys)."""
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]
