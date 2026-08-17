"""Argon2-hashed API keys. Plaintext is never stored or logged."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

KEY_TOKEN_PREFIX = "rlk_"
KEY_PREFIX_LEN = 12
WEBHOOK_SECRET_PREFIX = "whsec_"
VALID_ROLES = frozenset({"ingest", "query", "admin"})
DEFAULT_SCOPE = "default"

_hasher = PasswordHasher()


@dataclass(frozen=True)
class ApiKeyInfo:
    """Public key metadata — never includes the hash or plaintext secrets."""

    id: uuid.UUID
    name: str | None
    key_prefix: str
    role: str
    scope: str
    revoked_at: datetime | None
    created_at: datetime | None
    webhook_secret_preview: str | None = None
    allow_scope_override: bool = False


@dataclass(frozen=True)
class ApiKeyRecord:
    """Lookup result used by middleware. Hash is for verify only; do not log it."""

    id: uuid.UUID
    name: str | None
    key_prefix: str
    key_hash: str
    role: str
    scope: str
    revoked_at: datetime | None
    created_at: datetime | None = None
    allow_scope_override: bool = False


def generate_api_key() -> str:
    """Return a new plaintext key (`rlk_` + url-safe secret). Show it once."""
    return KEY_TOKEN_PREFIX + secrets.token_urlsafe(32)


def generate_webhook_secret() -> str:
    """Return a per-key HMAC secret (`whsec_` + url-safe). Store server-side."""
    return WEBHOOK_SECRET_PREFIX + secrets.token_urlsafe(32)


def mask_webhook_secret(secret: str | None) -> str | None:
    """List/display form — prefix only, never the full signing secret."""
    if not secret:
        return None
    return "whsec_****"


def key_prefix(token: str) -> str:
    """Indexed lookup prefix — first 12 characters, not the full secret."""
    return token[:KEY_PREFIX_LEN]


def hash_api_key(token: str) -> str:
    """Argon2 hash of the plaintext key."""
    return _hasher.hash(token)


def verify_api_key(token: str, key_hash: str) -> bool:
    """Return True if `token` matches `key_hash`. Never logs either value."""
    try:
        return _hasher.verify(key_hash, token)
    except (VerifyMismatchError, InvalidHashError, TypeError, ValueError):
        return False


def find_verified_key(
    token: str, candidates: Sequence[ApiKeyRecord]
) -> ApiKeyRecord | None:
    """Pick the non-revoked candidate whose prefix matches and whose hash verifies."""
    prefix = key_prefix(token)
    for candidate in candidates:
        if candidate.revoked_at is not None:
            continue
        if candidate.key_prefix != prefix:
            continue
        if verify_api_key(token, candidate.key_hash):
            return candidate
    return None


def lookup_api_key(token: str) -> ApiKeyRecord | None:
    """Load non-revoked rows by prefix and argon2-verify. Patchable in unit tests."""
    from sqlalchemy import select

    from src.db.models import ApiKey
    from src.db.session import get_db

    prefix = key_prefix(token)
    with get_db() as db:
        rows = (
            db.execute(
                select(ApiKey).where(
                    ApiKey.key_prefix == prefix,
                    ApiKey.revoked_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        records = [
            ApiKeyRecord(
                id=row.id,
                name=row.name,
                key_prefix=row.key_prefix,
                key_hash=row.key_hash,
                role=row.role,
                scope=row.scope,
                revoked_at=row.revoked_at,
                created_at=row.created_at,
                allow_scope_override=bool(getattr(row, "allow_scope_override", False)),
            )
            for row in rows
        ]
    return find_verified_key(token, records)


def create_api_key(
    *,
    role: str,
    scope: str = DEFAULT_SCOPE,
    name: str | None = None,
    allow_scope_override: bool = False,
) -> tuple[str, str, ApiKeyInfo]:
    """Persist a hashed key and return (api_key, webhook_secret, metadata).

    Caller must show both secrets once and must not log them. The webhook
    secret is stored plaintext so completions can be HMAC-signed; the API
    bearer token is argon2-hashed and cannot be used for HMAC.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
    if not scope:
        raise ValueError("scope must be a non-empty string")

    plaintext = generate_api_key()
    webhook_secret = generate_webhook_secret()
    record = _persist_key(
        plaintext=plaintext,
        webhook_secret=webhook_secret,
        role=role,
        scope=scope,
        name=name,
        allow_scope_override=allow_scope_override,
    )
    return plaintext, webhook_secret, record


def _key_info(row: Any) -> ApiKeyInfo:
    return ApiKeyInfo(
        id=row.id,
        name=row.name,
        key_prefix=row.key_prefix,
        role=row.role,
        scope=row.scope,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        webhook_secret_preview=mask_webhook_secret(
            getattr(row, "webhook_secret", None)
        ),
        allow_scope_override=bool(getattr(row, "allow_scope_override", False)),
    )


def _persist_key(
    *,
    plaintext: str,
    webhook_secret: str,
    role: str,
    scope: str,
    name: str | None,
    allow_scope_override: bool = False,
) -> ApiKeyInfo:
    from src.db.models import ApiKey
    from src.db.session import get_db

    row = ApiKey(
        name=name,
        key_prefix=key_prefix(plaintext),
        key_hash=hash_api_key(plaintext),
        role=role,
        scope=scope,
        webhook_secret=webhook_secret,
        allow_scope_override=allow_scope_override,
    )
    with get_db() as db:
        db.add(row)
        db.flush()
        return _key_info(row)


def list_api_keys() -> list[ApiKeyInfo]:
    """Return all keys (including revoked) without hashes."""
    from sqlalchemy import select

    from src.db.models import ApiKey
    from src.db.session import get_db

    with get_db() as db:
        rows = (
            db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
            .scalars()
            .all()
        )
        return [_key_info(row) for row in rows]


def revoke_api_key(key_id: uuid.UUID) -> ApiKeyInfo | None:
    """Set revoked_at. Returns the updated row, or None if the id is unknown."""
    from datetime import timezone

    from src.db.models import ApiKey
    from src.db.session import get_db

    with get_db() as db:
        row = db.get(ApiKey, key_id)
        if row is None:
            return None
        if row.revoked_at is None:
            row.revoked_at = datetime.now(timezone.utc)
        return _key_info(row)
