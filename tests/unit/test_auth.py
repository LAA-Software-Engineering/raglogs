"""Unit tests for HTTP API authentication (no database)."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.utils import to_base64url_uint

from src.api.app import app
from src.api.auth.bind_guard import InsecureBindError, is_loopback_host, warn_if_insecure_bind
from src.api.auth.keys import (
    ApiKeyRecord,
    create_api_key,
    find_verified_key,
    generate_api_key,
    hash_api_key,
    key_prefix,
    verify_api_key,
)
from src.api.auth.middleware import AuthPrincipal
from src.api.auth.oidc import (
    clear_jwks_cache,
    looks_like_jwt,
    role_from_claims,
    validate_oidc_token,
)
from src.api.auth.roles import required_roles
from src.config.settings import Settings

client = TestClient(app, raise_server_exceptions=False)

FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature"


def _auth_settings(**kwargs: object) -> Settings:
    values: dict[str, object] = {"auth_enabled": True, "auth_mode": "api_key"}
    values.update(kwargs)
    return Settings(_env_file=None, **values)


def _key(role: str, *, revoked: bool = False) -> MagicMock:
    record = MagicMock()
    record.id = uuid.uuid4()
    record.name = "test"
    record.key_prefix = "rlk_testhash"
    record.role = role
    record.scope = "default"
    record.revoked_at = datetime.now(timezone.utc) if revoked else None
    return record


# ── Key hashing / prefix ──────────────────────────────────────────────────────

class TestApiKeyCrypto:
    def test_generate_has_rlk_prefix(self) -> None:
        token = generate_api_key()
        assert token.startswith("rlk_")
        assert len(token) > 12

    def test_hash_does_not_equal_plaintext_and_verifies(self) -> None:
        token = generate_api_key()
        hashed = hash_api_key(token)
        assert hashed != token
        assert "argon2" in hashed.lower() or hashed.startswith("$argon2")
        assert verify_api_key(token, hashed) is True
        assert verify_api_key("rlk_not-the-key", hashed) is False

    def test_prefix_is_first_12_chars(self) -> None:
        token = generate_api_key()
        assert key_prefix(token) == token[:12]
        assert len(key_prefix(token)) == 12

    def test_prefix_lookup_helper_verifies_matching_candidate(self) -> None:
        token = generate_api_key()
        other = generate_api_key()
        matching = ApiKeyRecord(
            id=uuid.uuid4(),
            name="ok",
            key_prefix=key_prefix(token),
            key_hash=hash_api_key(token),
            role="query",
            scope="default",
            revoked_at=None,
        )
        distractor = ApiKeyRecord(
            id=uuid.uuid4(),
            name="other",
            key_prefix=key_prefix(other),
            key_hash=hash_api_key(other),
            role="admin",
            scope="default",
            revoked_at=None,
        )
        assert find_verified_key(token, [distractor, matching]) is matching

    def test_prefix_lookup_skips_revoked(self) -> None:
        token = generate_api_key()
        revoked = ApiKeyRecord(
            id=uuid.uuid4(),
            name="revoked",
            key_prefix=key_prefix(token),
            key_hash=hash_api_key(token),
            role="admin",
            scope="default",
            revoked_at=datetime.now(timezone.utc),
        )
        assert find_verified_key(token, [revoked]) is None

    def test_create_rejects_unknown_role_before_db(self) -> None:
        with pytest.raises(ValueError, match="role"):
            create_api_key(role="superuser")


# ── Role map ──────────────────────────────────────────────────────────────────

class TestRequiredRoles:
    def test_health_and_metrics_exempt(self) -> None:
        assert required_roles("GET", "/health") is None
        assert required_roles("GET", "/health/") is None
        assert required_roles("GET", "/metrics") is None
        assert required_roles("GET", "/metrics/prom") is None

    def test_ingest_post_vs_get(self) -> None:
        assert required_roles("POST", "/ingestions") == frozenset({"ingest", "admin"})
        assert required_roles("GET", "/ingestions") == frozenset({"query", "admin"})
        assert required_roles("GET", "/ingestions/latest") == frozenset({"query", "admin"})

    def test_query_and_config_and_ui(self) -> None:
        assert required_roles("POST", "/query/explain") == frozenset({"query", "admin"})
        assert required_roles("GET", "/config") == frozenset({"admin"})
        assert required_roles("GET", "/") == frozenset({"query", "admin"})
        assert required_roles("GET", "/static/js/app.js") == frozenset({"query", "admin"})


# ── Bind-host guard ───────────────────────────────────────────────────────────

class TestBindGuard:
    def test_loopback_hosts(self) -> None:
        assert is_loopback_host("127.0.0.1") is True
        assert is_loopback_host("localhost") is True
        assert is_loopback_host("::1") is True
        assert is_loopback_host("[::1]") is True
        assert is_loopback_host("127.0.0.2") is True

    def test_non_loopback_hosts(self) -> None:
        assert is_loopback_host("0.0.0.0") is False
        assert is_loopback_host("::") is False
        assert is_loopback_host("*") is False
        assert is_loopback_host("") is False
        assert is_loopback_host("8.8.8.8") is False

    def test_loopback_does_not_warn_when_auth_off(self) -> None:
        settings = Settings(auth_enabled=False, auth_refuse_insecure_bind=True, _env_file=None)
        with patch("structlog.get_logger") as mock_log:
            warn_if_insecure_bind("127.0.0.1", settings)
        mock_log.return_value.warning.assert_not_called()

    def test_non_loopback_warns_when_auth_off(self) -> None:
        settings = Settings(auth_enabled=False, auth_refuse_insecure_bind=False, _env_file=None)
        with patch("structlog.get_logger") as mock_log:
            warn_if_insecure_bind("0.0.0.0", settings)
        mock_log.return_value.warning.assert_called_once()

    def test_refuse_flag_raises_on_non_loopback(self) -> None:
        settings = Settings(auth_enabled=False, auth_refuse_insecure_bind=True, _env_file=None)
        with pytest.raises(InsecureBindError):
            warn_if_insecure_bind("0.0.0.0", settings)

    def test_auth_enabled_skips_guard(self) -> None:
        settings = Settings(auth_enabled=True, auth_refuse_insecure_bind=True, _env_file=None)
        with patch("structlog.get_logger") as mock_log:
            warn_if_insecure_bind("0.0.0.0", settings)
        mock_log.return_value.warning.assert_not_called()


# ── Middleware / TestClient ───────────────────────────────────────────────────

class TestAuthMiddleware:
    def test_health_ok_without_authorization_when_auth_enabled(self) -> None:
        with patch("src.config.get_settings", return_value=_auth_settings()), \
             patch("src.db.session.check_connection", return_value=False):
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_metrics_path_exempt_when_auth_enabled(self) -> None:
        with patch("src.config.get_settings", return_value=_auth_settings()):
            resp = client.get("/metrics")
        assert resp.status_code not in {401, 403}

    def test_explain_401_without_header_when_auth_enabled(self) -> None:
        with patch("src.config.get_settings", return_value=_auth_settings()):
            resp = client.post("/query/explain", json={"since": "1h"})
        assert resp.status_code == 401
        body = resp.json()
        assert body["error_code"] == "AUTH_UNAUTHORIZED"
        assert "message" in body

    def test_invalid_bearer_401_typed_body(self) -> None:
        with patch("src.config.get_settings", return_value=_auth_settings()), \
             patch("src.api.auth.keys.lookup_api_key", return_value=None):
            resp = client.post(
                "/query/explain",
                json={"since": "1h"},
                headers={"Authorization": "Bearer rlk_not-a-real-key"},
            )
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "AUTH_UNAUTHORIZED"

    def test_revoked_key_is_401(self) -> None:
        with patch("src.config.get_settings", return_value=_auth_settings()), \
             patch("src.api.auth.keys.lookup_api_key", return_value=None):
            resp = client.post(
                "/query/explain",
                json={"since": "1h"},
                headers={"Authorization": "Bearer rlk_revokedkeyxx"},
            )
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "AUTH_UNAUTHORIZED"

    def test_ingest_role_cannot_post_explain(self) -> None:
        with patch("src.config.get_settings", return_value=_auth_settings()), \
             patch("src.api.auth.keys.lookup_api_key", return_value=_key("ingest")):
            resp = client.post(
                "/query/explain",
                json={"since": "1h"},
                headers={"Authorization": "Bearer rlk_ingestrole1"},
            )
        assert resp.status_code == 403
        assert resp.json()["error_code"] == "AUTH_FORBIDDEN"

    def test_query_role_can_post_explain(self) -> None:
        with patch("src.config.get_settings", return_value=_auth_settings()), \
             patch("src.api.auth.keys.lookup_api_key", return_value=_key("query")):
            resp = client.post(
                "/query/explain",
                json={},
                headers={"Authorization": "Bearer rlk_queryrolexx"},
            )
        assert resp.status_code == 400
        assert resp.status_code != 403

    def test_query_role_cannot_post_ingestions(self) -> None:
        with patch("src.config.get_settings", return_value=_auth_settings()), \
             patch("src.api.auth.keys.lookup_api_key", return_value=_key("query")):
            resp = client.post(
                "/ingestions",
                json={"paths": ["/logs"]},
                headers={"Authorization": "Bearer rlk_queryrolexx"},
            )
        assert resp.status_code == 403
        assert resp.json()["error_code"] == "AUTH_FORBIDDEN"

    def test_ingest_role_can_post_ingestions(self) -> None:
        with patch("src.config.get_settings", return_value=_auth_settings()), \
             patch("src.api.auth.keys.lookup_api_key", return_value=_key("ingest")):
            resp = client.post(
                "/ingestions",
                json={},
                headers={"Authorization": "Bearer rlk_ingestrole1"},
            )
        assert resp.status_code == 422
        assert resp.status_code != 403

    def test_admin_allowed_on_explain_and_ingestions(self) -> None:
        with patch("src.config.get_settings", return_value=_auth_settings()), \
             patch("src.api.auth.keys.lookup_api_key", return_value=_key("admin")):
            explain = client.post(
                "/query/explain",
                json={},
                headers={"Authorization": "Bearer rlk_adminrolexx"},
            )
            ingest = client.post(
                "/ingestions",
                json={},
                headers={"Authorization": "Bearer rlk_adminrolexx"},
            )
            config = client.get(
                "/config",
                headers={"Authorization": "Bearer rlk_adminrolexx"},
            )
        assert explain.status_code == 400
        assert ingest.status_code == 422
        assert config.status_code == 200

    def test_query_role_cannot_get_config(self) -> None:
        with patch("src.config.get_settings", return_value=_auth_settings()), \
             patch("src.api.auth.keys.lookup_api_key", return_value=_key("query")):
            resp = client.get("/config", headers={"Authorization": "Bearer rlk_queryrolexx"})
        assert resp.status_code == 403

    def test_docs_not_exempt(self) -> None:
        with patch("src.config.get_settings", return_value=_auth_settings()):
            resp = client.get("/docs")
        assert resp.status_code == 401

    def test_default_auth_off_allows_query_without_header(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.auth_enabled is False
        with patch("src.config.get_settings", return_value=settings):
            resp = client.post("/query/explain", json={})
        assert resp.status_code == 400


class TestOidcMiddleware:
    def test_mocked_jwt_accepted_when_mode_oidc(self) -> None:
        principal = AuthPrincipal(role="query", scope="default", auth_method="oidc", subject="user-1")
        settings = _auth_settings(auth_mode="oidc", oidc_issuer="https://idp.example")
        with patch("src.config.get_settings", return_value=settings), \
             patch("src.api.auth.oidc.validate_oidc_token", return_value=principal):
            resp = client.post(
                "/query/explain",
                json={},
                headers={"Authorization": f"Bearer {FAKE_JWT}"},
            )
        assert resp.status_code == 400

    def test_jwt_rejected_when_mode_api_key(self) -> None:
        settings = _auth_settings(auth_mode="api_key")
        with patch("src.config.get_settings", return_value=settings), \
             patch("src.api.auth.oidc.validate_oidc_token") as mock_oidc:
            resp = client.post(
                "/query/explain",
                json={},
                headers={"Authorization": f"Bearer {FAKE_JWT}"},
            )
        mock_oidc.assert_not_called()
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "AUTH_UNAUTHORIZED"


# ── OIDC helpers (mocked JWKS, no live IdP) ───────────────────────────────────

def _rsa_jwk_and_token(
    *,
    issuer: str,
    audience: str | None = None,
    role_claim: dict | None = None,
    kid: str = "test-key",
) -> tuple[dict, str, rsa.RSAPrivateKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()

    def _b64(value: int) -> str:
        encoded = to_base64url_uint(value)
        return encoded.decode("ascii") if isinstance(encoded, bytes) else encoded

    jwk = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64(numbers.n),
        "e": _b64(numbers.e),
    }
    claims: dict = {
        "sub": "user-1",
        "iss": issuer,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    if audience:
        claims["aud"] = audience
    if role_claim:
        claims.update(role_claim)
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})
    return jwk, token, private_key


class TestOidcValidation:
    def setup_method(self) -> None:
        clear_jwks_cache()

    def teardown_method(self) -> None:
        clear_jwks_cache()

    def test_looks_like_jwt(self) -> None:
        assert looks_like_jwt(FAKE_JWT) is True
        assert looks_like_jwt("rlk_abc") is False
        assert looks_like_jwt("only.two") is False

    def test_role_from_claims(self) -> None:
        assert role_from_claims({"raglogs_role": "admin"}) == "admin"
        assert role_from_claims({"roles": ["ingest", "query"]}) == "ingest"
        assert role_from_claims({}) == "query"

    def test_valid_jwt_with_mocked_jwks(self) -> None:
        issuer = "https://idp.example"
        jwk, token, _ = _rsa_jwk_and_token(issuer=issuer, role_claim={"raglogs_role": "admin"})
        settings = SimpleNamespace(
            oidc_issuer=issuer,
            oidc_audience="",
            oidc_jwks_url="https://idp.example/.well-known/jwks.json",
        )
        with patch("src.api.auth.oidc._http_get_json", return_value={"keys": [jwk]}):
            principal = validate_oidc_token(token, settings)
        assert principal is not None
        assert principal.role == "admin"
        assert principal.subject == "user-1"

    def test_expired_jwt_rejected(self) -> None:
        issuer = "https://idp.example"
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = private_key.public_key().public_numbers()
        encoded_n = to_base64url_uint(numbers.n)
        encoded_e = to_base64url_uint(numbers.e)
        jwk = {
            "kty": "RSA",
            "kid": "test-key",
            "n": encoded_n.decode("ascii") if isinstance(encoded_n, bytes) else encoded_n,
            "e": encoded_e.decode("ascii") if isinstance(encoded_e, bytes) else encoded_e,
        }
        token = jwt.encode(
            {"sub": "user-1", "iss": issuer, "exp": int(time.time()) - 10},
            private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        settings = SimpleNamespace(
            oidc_issuer=issuer,
            oidc_audience="",
            oidc_jwks_url="https://idp.example/.well-known/jwks.json",
        )
        with patch("src.api.auth.oidc._http_get_json", return_value={"keys": [jwk]}):
            assert validate_oidc_token(token, settings) is None

    def test_wrong_issuer_rejected(self) -> None:
        jwk, token, _ = _rsa_jwk_and_token(issuer="https://idp.example")
        settings = SimpleNamespace(
            oidc_issuer="https://other.example",
            oidc_audience="",
            oidc_jwks_url="https://idp.example/.well-known/jwks.json",
        )
        with patch("src.api.auth.oidc._http_get_json", return_value={"keys": [jwk]}):
            assert validate_oidc_token(token, settings) is None
