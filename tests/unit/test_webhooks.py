"""Unit tests for HMAC-signed ingest completion webhooks (no live HTTP / DB)."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from tenacity import wait_none

from src.config.settings import Settings
from src.core.ingestion.webhooks import (
    SIGNATURE_HEADER,
    InvalidCallbackUrl,
    build_callback_payload,
    default_webhook_wait,
    deliver_callback,
    encode_payload,
    load_signing_secret,
    maybe_deliver_ingest_callback,
    sign_body,
    terminal_callback_status,
    validate_callback_url,
    verify_signature,
)


# ── Signing ───────────────────────────────────────────────────────────────────


class TestSignBody:
    def test_stable_hmac_hex(self) -> None:
        body = b'{"job_id":"abc","status":"succeeded"}'
        first = sign_body("whsec_test", body)
        second = sign_body("whsec_test", body)
        expected_hex = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
        assert first == second
        assert first == f"sha256={expected_hex}"
        assert verify_signature("whsec_test", body, first) is True

    def test_tampered_body_does_not_verify(self) -> None:
        body = b'{"job_id":"abc"}'
        signature = sign_body("whsec_test", body)
        assert verify_signature("whsec_test", b'{"job_id":"abd"}', signature) is False
        assert verify_signature("other-secret", body, signature) is False

    def test_payload_json_does_not_include_signature(self) -> None:
        payload = build_callback_payload(
            job_id="ing-1",
            status="succeeded",
            scope="default",
            lines=10,
        )
        assert "signature" not in payload
        raw = encode_payload(payload)
        assert b"signature" not in raw


# ── URL allowlist ─────────────────────────────────────────────────────────────


class TestValidateCallbackUrl:
    def test_https_ok(self) -> None:
        assert (
            validate_callback_url("https://hooks.example.com/ingest")
            == "https://hooks.example.com/ingest"
        )

    def test_http_ok(self) -> None:
        assert validate_callback_url(" http://localhost:8080/cb ") == (
            "http://localhost:8080/cb"
        )

    def test_rejects_file_scheme(self) -> None:
        with pytest.raises(InvalidCallbackUrl, match="http or https"):
            validate_callback_url("file:///etc/passwd")

    def test_rejects_javascript_scheme(self) -> None:
        with pytest.raises(InvalidCallbackUrl, match="http or https"):
            validate_callback_url("javascript:alert(1)")

    def test_rejects_empty_host(self) -> None:
        with pytest.raises(InvalidCallbackUrl, match="host"):
            validate_callback_url("https://")

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(InvalidCallbackUrl):
            validate_callback_url("")
        with pytest.raises(InvalidCallbackUrl):
            validate_callback_url("   ")


# ── Status mapping ────────────────────────────────────────────────────────────


class TestTerminalStatus:
    def test_done_succeeded(self) -> None:
        assert terminal_callback_status("done", 0) == ("succeeded", False)

    def test_done_with_errors_is_partial(self) -> None:
        assert terminal_callback_status("done", 3) == ("partial", True)

    def test_failed(self) -> None:
        assert terminal_callback_status("failed", 0) == ("failed", False)


# ── Delivery (mocked httpx) ───────────────────────────────────────────────────


def _response(
    status_code: int, url: str = "https://hooks.example.com/cb"
) -> httpx.Response:
    return httpx.Response(
        status_code,
        request=httpx.Request("POST", url),
    )


def _settings(**kwargs: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": "global-secret",
        "webhook_max_retries": 5,
        "webhook_timeout": 10.0,
    }
    values.update(kwargs)
    return Settings(_env_file=None, **values)


class TestDeliverCallback:
    def test_retries_500_then_succeeds(self) -> None:
        url = "https://hooks.example.com/cb"
        client = MagicMock()
        client.post.side_effect = [_response(500, url), _response(200, url)]
        settings = _settings()

        with patch("src.config.get_settings", return_value=settings):
            ok = deliver_callback(
                url,
                {"job_id": "j1", "status": "succeeded"},
                "whsec_test",
                client=client,
                wait=wait_none(),
            )

        assert ok is True
        assert client.post.call_count == 2
        headers = client.post.call_args.kwargs["headers"]
        assert SIGNATURE_HEADER in headers
        assert headers[SIGNATURE_HEADER].startswith("sha256=")

    def test_400_is_not_retried(self) -> None:
        url = "https://hooks.example.com/cb"
        client = MagicMock()
        client.post.return_value = _response(400, url)
        settings = _settings()

        with patch("src.config.get_settings", return_value=settings):
            ok = deliver_callback(
                url,
                {"job_id": "j1"},
                "whsec_test",
                client=client,
                wait=wait_none(),
            )

        assert ok is False
        assert client.post.call_count == 1

    def test_fail_open_after_retries_exhausted(self) -> None:
        url = "https://hooks.example.com/cb"
        client = MagicMock()
        client.post.return_value = _response(503, url)
        settings = _settings(webhook_max_retries=2)

        with patch("src.config.get_settings", return_value=settings):
            ok = deliver_callback(
                url,
                {"job_id": "j1"},
                "whsec_test",
                client=client,
                wait=wait_none(),
                max_retries=2,
            )

        assert ok is False
        assert client.post.call_count == 3  # 1 initial + 2 retries

    def test_connect_error_retries_then_fail_open(self) -> None:
        client = MagicMock()
        client.post.side_effect = httpx.ConnectError("refused")
        settings = _settings()

        with patch("src.config.get_settings", return_value=settings):
            ok = deliver_callback(
                "https://hooks.example.com/cb",
                {"job_id": "j1"},
                "whsec_test",
                client=client,
                wait=wait_none(),
                max_retries=1,
            )

        assert ok is False
        assert client.post.call_count == 2

    def test_429_is_retried(self) -> None:
        url = "https://hooks.example.com/cb"
        client = MagicMock()
        client.post.side_effect = [_response(429, url), _response(204, url)]
        settings = _settings()

        with patch("src.config.get_settings", return_value=settings):
            ok = deliver_callback(
                url,
                {"job_id": "j1"},
                "whsec_test",
                client=client,
                wait=wait_none(),
            )

        assert ok is True
        assert client.post.call_count == 2

    def test_default_wait_is_exponential_jitter(self) -> None:
        wait = default_webhook_wait()
        assert wait.initial == 0.5
        assert wait.max == 8
        assert getattr(wait, "jitter", None) == 1


class TestLoadSigningSecret:
    def test_per_key_secret_preferred(self) -> None:
        key_id = uuid.uuid4()
        row = SimpleNamespace(webhook_secret="whsec_from_key")
        db = MagicMock()
        db.get.return_value = row
        settings = _settings(webhook_secret="global")
        with patch("src.config.get_settings", return_value=settings):
            assert load_signing_secret(db, str(key_id)) == "whsec_from_key"

    def test_falls_back_to_global_when_key_secret_null(self) -> None:
        key_id = uuid.uuid4()
        db = MagicMock()
        db.get.return_value = SimpleNamespace(webhook_secret=None)
        settings = _settings(webhook_secret="global-secret")
        with patch("src.config.get_settings", return_value=settings):
            assert load_signing_secret(db, str(key_id)) == "global-secret"

    def test_global_when_no_key(self) -> None:
        settings = _settings(webhook_secret="global-secret")
        with patch("src.config.get_settings", return_value=settings):
            assert load_signing_secret(MagicMock(), None) == "global-secret"


class TestMaybeDeliver:
    def test_no_callback_url_skips_http(self) -> None:
        job = SimpleNamespace(
            id=uuid.uuid4(),
            status="done",
            payload_json={"paths": ["/logs"]},
            result_json={"parsed_count": 10},
            ingestion_job_id=uuid.uuid4(),
        )
        with patch("src.core.ingestion.webhooks.deliver_callback") as mock_deliver:
            maybe_deliver_ingest_callback(MagicMock(), job)
        mock_deliver.assert_not_called()

    def test_done_invokes_deliver_with_ingestion_job_id(self) -> None:
        ingestion_id = uuid.uuid4()
        job = SimpleNamespace(
            id=uuid.uuid4(),
            status="done",
            payload_json={
                "callback_url": "https://hooks.example.com/cb",
                "scope": "incident:INC-1",
            },
            result_json={"parsed_count": 42, "error_count": 0, "lines_read": 50},
            ingestion_job_id=ingestion_id,
        )
        settings = _settings(webhook_secret="global-secret")
        with (
            patch("src.config.get_settings", return_value=settings),
            patch("src.core.ingestion.webhooks.deliver_callback") as mock_deliver,
        ):
            maybe_deliver_ingest_callback(MagicMock(), job)

        mock_deliver.assert_called_once()
        url, payload, secret = mock_deliver.call_args.args[:3]
        assert url == "https://hooks.example.com/cb"
        assert payload["job_id"] == str(ingestion_id)
        assert payload["status"] == "succeeded"
        assert payload["scope"] == "incident:INC-1"
        assert payload["counts"]["lines"] == 42
        assert payload["counts"]["clusters"] == 0
        assert payload["partial"] is False
        assert secret == "global-secret"

    def test_failed_invokes_deliver(self) -> None:
        job = SimpleNamespace(
            id="worker-1",
            status="failed",
            payload_json={"callback_url": "https://hooks.example.com/cb"},
            result_json=None,
            ingestion_job_id=None,
        )
        settings = _settings(webhook_secret="global-secret")
        with (
            patch("src.config.get_settings", return_value=settings),
            patch("src.core.ingestion.webhooks.deliver_callback") as mock_deliver,
        ):
            maybe_deliver_ingest_callback(MagicMock(), job)

        payload = mock_deliver.call_args.args[1]
        assert payload["status"] == "failed"
        assert payload["job_id"] == "worker-1"
        assert payload["partial"] is False

    def test_skips_when_no_secret(self) -> None:
        job = SimpleNamespace(
            id=uuid.uuid4(),
            status="done",
            payload_json={"callback_url": "https://hooks.example.com/cb"},
            result_json={"parsed_count": 1},
            ingestion_job_id=uuid.uuid4(),
        )
        settings = _settings(webhook_secret="")
        with (
            patch("src.config.get_settings", return_value=settings),
            patch("src.core.ingestion.webhooks.deliver_callback") as mock_deliver,
        ):
            maybe_deliver_ingest_callback(MagicMock(), job)
        mock_deliver.assert_not_called()
