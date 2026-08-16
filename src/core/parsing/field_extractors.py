from typing import Any, Optional

# Field aliases for common log formats
TIMESTAMP_FIELDS = ["timestamp", "ts", "time", "@timestamp", "datetime", "date", "log_timestamp"]
MESSAGE_FIELDS = ["message", "msg", "log", "text", "body", "content", "log_message"]
LEVEL_FIELDS = ["level", "severity", "log_level", "loglevel", "lvl", "priority"]
SERVICE_FIELDS = ["service", "app", "logger", "component", "application", "service_name", "app_name", "source"]
ENVIRONMENT_FIELDS = ["environment", "env", "deployment", "stage", "namespace"]
TRACE_ID_FIELDS = ["trace_id", "traceId", "trace", "x_trace_id", "X-Trace-Id"]
REQUEST_ID_FIELDS = ["request_id", "requestId", "req_id", "reqId", "correlation_id", "correlationId"]
HOST_FIELDS = ["host", "hostname", "server", "instance", "pod", "node"]

LEVEL_NORMALIZATIONS = {
    "fatal": "fatal",
    "critical": "fatal",
    "crit": "fatal",
    "error": "error",
    "err": "error",
    "warn": "warn",
    "warning": "warn",
    "info": "info",
    "information": "info",
    "informational": "info",
    "debug": "debug",
    "dbg": "debug",
    "trace": "debug",
    "verbose": "debug",
}


def extract_field(data: dict[str, Any], field_names: list[str]) -> Optional[Any]:
    """Extract the first matching field from a dict using multiple alias names."""
    for field in field_names:
        if field in data:
            return data[field]
        # Try case-insensitive
        lower = field.lower()
        for key in data:
            if key.lower() == lower:
                return data[key]
    return None


def normalize_level(level_str: str) -> str:
    """Normalize level string to a standard form."""
    if not level_str:
        return "info"
    return LEVEL_NORMALIZATIONS.get(level_str.lower().strip(), level_str.lower().strip())


def extract_kubernetes_fields(data: dict[str, Any]) -> dict[str, Optional[Any]]:
    """Map a nested Fluent Bit / Vector `kubernetes` object onto raglogs fields.

    service  ← labels.app | labels['app.kubernetes.io/name'] | container_name
    environment ← namespace_name | pod_namespace | namespace
    host     ← pod_name | pod | host
    """
    k8s = data.get("kubernetes")
    if not isinstance(k8s, dict):
        return {}

    labels = k8s.get("labels") or k8s.get("pod_labels") or {}
    service: Optional[Any] = None
    if isinstance(labels, dict):
        service = (
            labels.get("app")
            or labels.get("app.kubernetes.io/name")
            or labels.get("k8s-app")
        )
    service = service or k8s.get("container_name") or k8s.get("container")

    environment = (
        k8s.get("namespace_name") or k8s.get("pod_namespace") or k8s.get("namespace")
    )
    host = k8s.get("pod_name") or k8s.get("pod") or k8s.get("host")

    return {"service": service, "environment": environment, "host": host}


def extract_all_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Extract all known fields from a parsed log dict."""
    known_fields = set(
        TIMESTAMP_FIELDS
        + MESSAGE_FIELDS
        + LEVEL_FIELDS
        + SERVICE_FIELDS
        + ENVIRONMENT_FIELDS
        + TRACE_ID_FIELDS
        + REQUEST_ID_FIELDS
        + HOST_FIELDS
    )

    extra = {k: v for k, v in data.items() if k not in known_fields}
    k8s_fields = extract_kubernetes_fields(data)

    level_raw = extract_field(data, LEVEL_FIELDS)
    level = normalize_level(str(level_raw)) if level_raw else None

    return {
        "timestamp_raw": extract_field(data, TIMESTAMP_FIELDS),
        "message": extract_field(data, MESSAGE_FIELDS),
        "level": level,
        "service": extract_field(data, SERVICE_FIELDS) or k8s_fields.get("service"),
        "environment": extract_field(data, ENVIRONMENT_FIELDS) or k8s_fields.get("environment"),
        "trace_id": extract_field(data, TRACE_ID_FIELDS),
        "request_id": extract_field(data, REQUEST_ID_FIELDS),
        "host": extract_field(data, HOST_FIELDS) or k8s_fields.get("host"),
        "extra": extra,
    }
