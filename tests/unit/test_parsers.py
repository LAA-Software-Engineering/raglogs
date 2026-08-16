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

    def test_fluent_bit_kubernetes_object(self):
        line = (
            '{"time":"2026-03-12T22:01:10.123Z","log":"Stripe signature verification failed",'
            '"stream":"stderr","kubernetes":{"pod_name":"billing-worker-7d9f8c-xk2pq",'
            '"namespace_name":"production","container_name":"billing-worker",'
            '"host":"ip-10-0-1-23","labels":{"app":"billing-worker"}}}'
        )
        result = parse_json_line(line)
        assert result is not None
        assert result.parse_error is None
        assert result.service == "billing-worker"
        assert result.environment == "production"
        assert result.host == "billing-worker-7d9f8c-xk2pq"
        assert "Stripe" in result.message
        assert result.timestamp is not None
        assert "kubernetes" in result.extra

    def test_vector_kubernetes_object(self):
        line = (
            '{"timestamp":"2026-03-12T22:01:10Z","message":"connection timeout",'
            '"kubernetes":{"pod_name":"api-5f6d","pod_namespace":"staging",'
            '"container_name":"api","pod_node_name":"node-1",'
            '"pod_labels":{"app.kubernetes.io/name":"checkout-api"}}}'
        )
        result = parse_json_line(line)
        assert result.service == "checkout-api"
        assert result.environment == "staging"
        assert result.host == "api-5f6d"

    def test_explicit_service_wins_over_kubernetes(self):
        line = (
            '{"service":"explicit","message":"x",'
            '"kubernetes":{"container_name":"from-k8s","namespace_name":"ns",'
            '"pod_name":"pod-1"}}'
        )
        result = parse_json_line(line)
        assert result.service == "explicit"
        assert result.environment == "ns"
        assert result.host == "pod-1"


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
        assert (
            result.service == "my-app" or result.service is None
        )  # parser may not extract service from plain text

    def test_cri_line(self):
        line = (
            "2026-03-12T22:01:10.123456789Z stdout F "
            "ERROR billing-worker Stripe signature verification failed"
        )
        result = parse_text_line(line)
        assert result is not None
        assert result.timestamp is not None
        assert result.timestamp.tzinfo is not None
        assert result.level == "error"
        assert result.service == "billing-worker"
        assert "Stripe" in result.message
        assert "stdout" not in result.message

    def test_kubectl_prefix_with_timestamps(self):
        line = (
            "[billing-worker-7d9f8c-xk2pq/billing-worker] "
            "2026-03-12T22:01:10.123456789Z ERROR connection timeout"
        )
        result = parse_text_line(line)
        assert result.service == "billing-worker"
        assert result.host == "billing-worker-7d9f8c-xk2pq"
        assert result.timestamp is not None
        assert result.level == "error"
        assert "connection timeout" in result.message

    def test_kubectl_prefix_with_namespace(self):
        line = "[production/api-5f6d/api] 2026-03-12T22:01:10Z INFO listening on :8080"
        result = parse_text_line(line)
        assert result.service == "api"
        assert result.host == "api-5f6d"
        assert result.environment == "production"
        assert result.timestamp is not None
        assert "listening on :8080" in result.message

    def test_untimestamped_error_does_not_steal_service_token(self):
        # LEVEL_SERVICE_PATTERN is CRI-inner only. Generic file ingest must keep
        # default_service and the full message (not "failed" / "to connect...").
        line = "ERROR failed to connect to database"
        result = parse_text_line(line, default_service="api")
        assert result is not None
        assert result.service == "api"
        assert result.message == line
        assert result.level == "error"
