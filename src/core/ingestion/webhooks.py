"""HMAC-signed ingest completion webhooks.

Optional ``callback_url`` on ``POST /v1/ingestions`` is stored on the worker
job. When the batch worker reaches a terminal state (done / failed), raglogs
POSTs a JSON body signed with HMAC-SHA256.

The signature is sent in ``X-Raglogs-Signature: sha256=<hex>`` — never inside
the JSON (a body cannot sign a field that contains its own signature).
Consumers HMAC the raw request body with the webhook secret and compare.

Signing secret resolution:
- per-key ``api_keys.webhook_secret`` (``whsec_…`` shown once at mint) when
  ``api_key_id`` is on the job payload
- otherwise ``WEBHOOK_SECRET`` (auth off, OIDC, or legacy keys with null secret)

Failures after bounded retries are logged; they never fail the ingest job.
Tail jobs and sync push ingest do not fire callbacks in this release.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import orjson
import structlog
from tenacity import (
    retry_if_exception,
    Retrying,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = structlog.get_logger()

SIGNATURE_HEADER = "X-Raglogs-Signature"
ALLOWED_CALLBACK_SCHEMES = frozenset({"http", "https"})
DEFAULT_SCOPE = "default"


class InvalidCallbackUrl(ValueError):
    """``callback_url`` failed the scheme/host allowlist."""


class NonRetryableWebhookError(Exception):
    """HTTP 4xx other than 429 — do not retry."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"webhook receiver returned {status_code}")


def validate_callback_url(url: str) -> str:
    """Return a stripped http(s) URL or raise ``InvalidCallbackUrl``.

    SSRF control is a scheme allowlist plus a required host. ``file:``,
    empty hosts, and non-http schemes are rejected. Loopback / link-local
    hosts are allowed so local receivers work in development.
    """
    stripped = (url or "").strip()
    if not stripped:
        raise InvalidCallbackUrl("callback_url must be a non-empty http(s) URL")
    parsed = urlparse(stripped)
    if parsed.scheme not in ALLOWED_CALLBACK_SCHEMES:
        raise InvalidCallbackUrl("callback_url must use http or https")
    if not parsed.hostname:
        raise InvalidCallbackUrl("callback_url must include a host")
    return stripped


def sign_body(secret: str, body: bytes) -> str:
    """Return ``sha256=<hex>`` HMAC-SHA256 of ``body`` with ``secret``."""
    digest = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header_value: str) -> bool:
    """Constant-time compare of ``header_value`` against HMAC of ``body``."""
    expected = sign_body(secret, body)
    return hmac.compare_digest(expected, header_value)


def default_webhook_wait() -> wait_exponential_jitter:
    """Jittered exponential backoff used between webhook POST attempts."""
    return wait_exponential_jitter(initial=0.5, max=8)


def encode_payload(payload: dict[str, Any]) -> bytes:
    """Serialize the webhook JSON body (compact UTF-8, no signature field)."""
    return orjson.dumps(payload)


def build_callback_payload(
    *,
    job_id: str,
    status: str,
    scope: str,
    lines: int,
    clusters: int = 0,
    partial: bool = False,
) -> dict[str, Any]:
    """Completion payload. ``job_id`` is the ingestion_job_id when present."""
    return {
        "job_id": job_id,
        "status": status,
        "scope": scope,
        "counts": {"lines": lines, "clusters": clusters},
        "partial": partial,
    }


def _safe_callback_host(url: str) -> str:
    try:
        return urlparse(url).hostname or "unknown"
    except Exception:
        return "unknown"


def _is_retryable_webhook_error(exc: BaseException) -> bool:
    if isinstance(exc, NonRetryableWebhookError):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def _post_callback(
    client: httpx.Client,
    url: str,
    body: bytes,
    signature: str,
    timeout: float,
) -> None:
    response = client.post(
        url,
        content=body,
        headers={
            "Content-Type": "application/json",
            SIGNATURE_HEADER: signature,
        },
        timeout=timeout,
    )
    if 400 <= response.status_code < 500 and response.status_code != 429:
        raise NonRetryableWebhookError(response.status_code)
    if response.status_code >= 400:
        response.raise_for_status()


def load_signing_secret(db: Any, api_key_id: Optional[str]) -> str:
    """Per-key webhook secret, else ``WEBHOOK_SECRET``. Never logs the value."""
    from src.config import get_settings

    if api_key_id:
        try:
            key_uuid = uuid.UUID(str(api_key_id))
        except ValueError:
            key_uuid = None
        if key_uuid is not None and db is not None:
            from src.db.models import ApiKey

            row = db.get(ApiKey, key_uuid)
            stored = getattr(row, "webhook_secret", None) if row is not None else None
            if isinstance(stored, str) and stored:
                return stored
    return get_settings().webhook_secret


def terminal_callback_status(
    worker_status: str,
    error_count: int,
) -> tuple[str, bool]:
    """Map worker terminal state to payload ``status`` and ``partial``."""
    if worker_status == "failed":
        return "failed", False
    if error_count > 0:
        return "partial", True
    return "succeeded", False


def deliver_callback(
    url: str,
    payload: dict[str, Any],
    secret: str,
    *,
    max_retries: Optional[int] = None,
    timeout: Optional[float] = None,
    client: Optional[httpx.Client] = None,
    wait: Any = None,
) -> bool:
    """POST ``payload`` to ``url`` with HMAC header. Fail-open: never raises.

    Retries 5xx, 429, and connect errors up to ``max_retries`` extra attempts.
    Other 4xx are not retried. Returns True on 2xx, False after giving up.
    """
    from src.config import get_settings

    settings = get_settings()
    retries = settings.webhook_max_retries if max_retries is None else max_retries
    request_timeout = settings.webhook_timeout if timeout is None else timeout
    retry_wait = wait if wait is not None else default_webhook_wait()

    body = encode_payload(payload)
    signature = sign_body(secret, body)
    job_id = payload.get("job_id")
    host = _safe_callback_host(url)

    owns_client = client is None
    http_client = client or httpx.Client(timeout=request_timeout)
    try:
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(retries + 1),
                wait=retry_wait,
                retry=retry_if_exception(_is_retryable_webhook_error),
                reraise=True,
            ):
                with attempt:
                    _post_callback(http_client, url, body, signature, request_timeout)
        except Exception as exc:
            extra: dict[str, Any] = {
                "host": host,
                "job_id": job_id,
                "error": type(exc).__name__,
            }
            if isinstance(exc, httpx.HTTPStatusError):
                extra["status_code"] = exc.response.status_code
            elif isinstance(exc, NonRetryableWebhookError):
                extra["status_code"] = exc.status_code
            log.warning("webhook_delivery_failed", **extra)
            return False
        log.info("webhook_delivered", host=host, job_id=job_id)
        return True
    finally:
        if owns_client:
            http_client.close()


def maybe_deliver_ingest_callback(db: Any, worker_job: Any) -> None:
    """Fire a completion webhook if the worker job stored a ``callback_url``.

    Must not raise: an exception here would roll back the worker job commit.
    """
    try:
        _maybe_deliver_ingest_callback(db, worker_job)
    except Exception:
        log.warning(
            "webhook_delivery_failed",
            worker_job_id=str(getattr(worker_job, "id", "")),
            error="unexpected",
        )


def _maybe_deliver_ingest_callback(db: Any, worker_job: Any) -> None:
    payload_json = getattr(worker_job, "payload_json", None) or {}
    if not isinstance(payload_json, dict):
        return
    raw_url = payload_json.get("callback_url")
    if not raw_url:
        return
    try:
        url = validate_callback_url(str(raw_url))
    except InvalidCallbackUrl:
        log.warning(
            "webhook_skipped_invalid_url",
            worker_job_id=str(getattr(worker_job, "id", "")),
        )
        return

    result = getattr(worker_job, "result_json", None) or {}
    if not isinstance(result, dict):
        result = {}
    error_count = int(result.get("error_count") or 0)
    parsed_count = int(result.get("parsed_count") or 0)
    lines_read = int(result.get("lines_read") or 0)
    lines = parsed_count if parsed_count else lines_read

    status, partial = terminal_callback_status(
        str(getattr(worker_job, "status", "")),
        error_count,
    )
    ingestion_job_id = getattr(worker_job, "ingestion_job_id", None)
    job_id = str(ingestion_job_id) if ingestion_job_id else str(worker_job.id)
    scope = str(payload_json.get("scope") or DEFAULT_SCOPE)
    api_key_id = payload_json.get("api_key_id")
    secret = load_signing_secret(db, str(api_key_id) if api_key_id else None)
    if not secret:
        log.warning(
            "webhook_skipped_no_secret",
            worker_job_id=str(getattr(worker_job, "id", "")),
        )
        return

    payload = build_callback_payload(
        job_id=job_id,
        status=status,
        scope=scope,
        lines=lines,
        partial=partial,
    )
    deliver_callback(url, payload, secret)
