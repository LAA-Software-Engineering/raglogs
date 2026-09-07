from src.core.normalization.normalize import normalize_message
from src.core.normalization.fingerprint import compute_fingerprint, fingerprint_message


class TestNormalize:
    def test_uuid_replacement(self):
        msg = "Processing user 550e8400-e29b-41d4-a716-446655440000 request"
        result = normalize_message(msg)
        assert "550e8400" not in result
        assert "<uuid>" in result

    def test_ip_replacement(self):
        msg = "Connection from 192.168.1.100:8080"
        result = normalize_message(msg)
        assert "192.168.1.100" not in result
        assert "<ip>" in result

    def test_email_replacement(self):
        msg = "Email sent to user@example.com"
        result = normalize_message(msg)
        assert "user@example.com" not in result
        assert "<email>" in result

    def test_numeric_id_in_message(self):
        msg1 = "User 123456789 failed login"
        msg2 = "User 987654321 failed login"
        norm1 = normalize_message(msg1)
        norm2 = normalize_message(msg2)
        assert norm1 == norm2

    def test_long_and_short_keyword_ids_share_fingerprint(self):
        # Regression for #64: an 8+ digit decimal ID must normalize to <id>,
        # not <hex>, so it clusters with shorter IDs instead of fragmenting.
        long_id = normalize_message("user 12345678 not found")
        short_id = normalize_message("user 4567 not found")
        assert long_id == "user <id> not found"
        assert long_id == short_id

    def test_keyword_id_before_hex_preserves_ip(self):
        # The keyword-ID rule must not run ahead of the IPv4 rule (it would eat
        # the first octet: "user <id>.168.1.1"). The IP stays intact and the
        # trailing decimal ID still normalizes.
        result = normalize_message("user 192.168.1.1 accessed order 12345678")
        assert result == "user <ip> accessed order <id>"

    def test_real_hex_hash_still_hex(self):
        # A hex string containing letters is still a hash, not an ID.
        assert normalize_message("commit abcdef1234567890 pushed") == "commit <hex> pushed"

    def test_endpoint_preserved(self):
        msg = "Stripe signature verification failed for endpoint /webhooks/stripe"
        result = normalize_message(msg)
        assert "/webhooks/stripe" in result

    def test_service_name_preserved(self):
        msg = "Connection timeout in billing-worker service"
        result = normalize_message(msg)
        assert "billing-worker" in result

    def test_status_code_preserved(self):
        msg = "POST /api/checkout 500 Internal Server Error"
        result = normalize_message(msg)
        assert "500" in result

    def test_empty_message(self):
        assert normalize_message("") == ""

    def test_whitespace_collapsed(self):
        msg = "Error   in   service"
        result = normalize_message(msg)
        assert "  " not in result


class TestFingerprint:
    def test_same_message_same_fingerprint(self):
        fp1 = compute_fingerprint("Stripe signature verification failed")
        fp2 = compute_fingerprint("Stripe signature verification failed")
        assert fp1 == fp2

    def test_different_messages_different_fingerprint(self):
        fp1 = compute_fingerprint("Stripe signature failed")
        fp2 = compute_fingerprint("Database connection timeout")
        assert fp1 != fp2

    def test_fingerprint_is_16_chars(self):
        fp = compute_fingerprint("test message")
        assert len(fp) == 16

    def test_similar_messages_same_fingerprint(self):
        msg1 = "User 123 failed login from 192.168.1.1"
        msg2 = "User 456 failed login from 10.0.0.1"
        _, fp1 = fingerprint_message(msg1)
        _, fp2 = fingerprint_message(msg2)
        assert fp1 == fp2

    def test_returns_normalized_and_fp(self):
        normalized, fp = fingerprint_message("Test message with UUID 550e8400-e29b-41d4-a716-446655440000")
        assert "<uuid>" in normalized
        assert len(fp) == 16
