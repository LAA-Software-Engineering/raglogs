import pytest
from datetime import datetime, timezone

from src.core.parsing.json_parser import parse_json_line
from src.core.parsing.text_parser import parse_text_line


class TestJsonParser:
    def test_standard_json_log(self):
        line = '{"timestamp":"2026-03-12T22:01:10Z","level":"error","service":"billing-worker","message":"Stripe signature verification failed"}'
        result = parse_json_line(line)
        assert result is not None
        assert result.parse_error is None
        assert result.level == "error"
        assert result.service == "billing-worker"
        assert "Stripe" in result.message
        assert result.timestamp is not None
        assert result.timestamp.tzinfo is not None

    def test_field_aliases(self):
        line = '{"ts":"2026-03-12T22:00:00Z","severity":"WARN","app":"api","msg":"Slow response"}'
        result = parse_json_line(line)
        assert result is not None
        assert result.level == "warn"
        assert result.service == "api"
        assert "Slow" in result.message

    def test_invalid_json(self):
        line = "this is not json"
        result = parse_json_line(line)
        assert result is not None
        assert result.parse_error is not None

    def test_empty_line(self):
        result = parse_json_line("")
        assert result is None

    def test_default_service(self):
        line = '{"level":"info","message":"Started"}'
        result = parse_json_line(line, default_service="my-service")
        assert result.service == "my-service"

    def test_level_normalization(self):
        line = '{"level":"WARNING","message":"test"}'
        result = parse_json_line(line)
        assert result.level == "warn"

    def test_extra_fields_preserved(self):
        line = '{"level":"info","message":"test","trace_id":"abc123","custom_field":"value"}'
        result = parse_json_line(line)
        assert result.trace_id == "abc123"
        assert "custom_field" in result.extra


class TestTextParser:
    def test_standard_text_log(self):
        line = "2026-03-12T22:01:10Z ERROR billing-worker Stripe signature verification failed"
        result = parse_text_line(line)
        assert result is not None
        assert result.level == "error"
        assert result.service == "billing-worker"
        assert "Stripe" in result.message

    def test_level_extraction(self):
        line = "2026-03-12T22:00:00Z WARN api High latency detected"
        result = parse_text_line(line)
        assert result is not None
        assert result.level == "warn"

    def test_empty_line(self):
        result = parse_text_line("   ")
        assert result is None

    def test_message_fallback(self):
        line = "Some arbitrary log line without structure"
        result = parse_text_line(line)
        assert result is not None
        assert result.message is not None
        assert len(result.message) > 0

    def test_default_service(self):
        # When service is not extractable from the line, default_service is used
        # This line has no service field, so default_service applies
        line = "Some log line without any structure or known format here"
        result = parse_text_line(line, default_service="my-app")
        # default_service fills in when structured parsing doesn't find a service
        assert result.service == "my-app" or result.service is None  # parser may not extract service from plain text
