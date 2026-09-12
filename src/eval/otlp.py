"""Convert OTLP-JSON (the OTel Collector's file exporter) into harness records (#79).

The OTel Demo ships a Collector; pointing its ``file`` exporter at a directory
writes OTLP-JSON — one JSON object per line — for logs, traces, and metrics. This
module parses those into the same records the eval harness and the multi-modal
feature layer already consume:

- logs   -> ``{timestamp, service, message, level}`` (the eval ``logs.jsonl`` shape)
- traces -> :class:`~src.core.ingestion.telemetry.ParsedSpan`
- metrics-> :class:`~src.core.ingestion.telemetry.ParsedMetricSample`

so the frozen RCAEval-trained ranker/calibrator run on OTel-Demo captures without
any format divergence. Pure functions over already-parsed OTLP dicts, plus a
:func:`capture_from_otlp_dir` that plugs into ``otel_demo.generate_incident``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.core.ingestion.telemetry import ParsedMetricSample, ParsedSpan
from src.eval.rcaeval import _infer_level

Window = Optional[tuple[datetime, datetime]]


def _attr(attributes, key: str) -> Optional[str]:
    """Read a string attribute value from an OTLP attribute list."""
    for a in attributes or []:
        if a.get("key") == key:
            v = a.get("value") or {}
            return v.get("stringValue") or v.get("stringvalue")
    return None


def _service(resource: dict) -> Optional[str]:
    return _attr((resource or {}).get("attributes"), "service.name")


def _dt(unix_nano: object) -> Optional[datetime]:
    """OTLP timestamps are unsigned nanoseconds since epoch (JSON strings)."""
    if unix_nano is None:
        return None
    try:
        return datetime.fromtimestamp(int(unix_nano) / 1e9, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _in_window(ts: Optional[datetime], window: Window) -> bool:
    if ts is None:
        return False
    return window is None or (window[0] <= ts <= window[1])


def _num(dp: dict) -> Optional[float]:
    v = dp.get("asDouble")
    if v is None:
        v = dp.get("asInt")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_otlp_logs(objs: list[dict], window: Window = None) -> list[dict]:
    recs: list[dict] = []
    for obj in objs:
        for rl in obj.get("resourceLogs") or []:
            service = _service(rl.get("resource") or {})
            for sl in rl.get("scopeLogs") or []:
                for lr in sl.get("logRecords") or []:
                    ts = _dt(lr.get("timeUnixNano") or lr.get("observedTimeUnixNano"))
                    if not _in_window(ts, window):
                        continue
                    body = lr.get("body") or {}
                    message = str(body.get("stringValue") or body.get("stringvalue") or "").strip()
                    sev = (lr.get("severityText") or "").strip().lower()
                    level = sev if sev in ("error", "fatal", "critical", "warn", "info", "debug") else _infer_level(message)
                    recs.append({
                        "timestamp": ts.isoformat(),
                        "service": service,
                        "message": message,
                        "level": level,
                    })
    return recs


def parse_otlp_traces(objs: list[dict], window: Window = None) -> list[ParsedSpan]:
    spans: list[ParsedSpan] = []
    for obj in objs:
        for rs in obj.get("resourceSpans") or []:
            service = _service(rs.get("resource") or {})
            for ss in rs.get("scopeSpans") or []:
                for sp in ss.get("spans") or []:
                    start = _dt(sp.get("startTimeUnixNano"))
                    if not _in_window(start, window):
                        continue
                    end = _dt(sp.get("endTimeUnixNano"))
                    duration_ms = (end - start).total_seconds() * 1000.0 if end and start else None
                    status = (sp.get("status") or {}).get("code")
                    spans.append(ParsedSpan(
                        trace_id=sp.get("traceId") or sp.get("traceid"),
                        span_id=sp.get("spanId") or sp.get("spanid"),
                        parent_span_id=sp.get("parentSpanId") or sp.get("parentspanid") or None,
                        service=service,
                        operation=sp.get("name"),
                        start_time=start,
                        duration_ms=duration_ms,
                        status_code=str(status) if status is not None else None,
                    ))
    return spans


def parse_otlp_metrics(objs: list[dict], window: Window = None) -> list[ParsedMetricSample]:
    samples: list[ParsedMetricSample] = []
    for obj in objs:
        for rm in obj.get("resourceMetrics") or []:
            service = _service(rm.get("resource") or {})
            for sm in rm.get("scopeMetrics") or []:
                for metric in sm.get("metrics") or []:
                    name = metric.get("name")
                    mtype, points = _metric_type_and_points(metric)
                    for dp in points:
                        ts = _dt(dp.get("timeUnixNano"))
                        if not _in_window(ts, window):
                            continue
                        value = _num(dp)
                        if value is None:
                            continue
                        samples.append(ParsedMetricSample(
                            service=service, metric=name, value=value, ts=ts, metric_type=mtype
                        ))
    return samples


def _is_cumulative(node: dict) -> bool:
    """OTLP ``aggregationTemporality``: cumulative (2) vs delta (1). Absent =
    cumulative (the OTel SDK/demo default). A *delta* monotonic sum is already a
    per-interval increment, so it must NOT be classified as a cumulative counter."""
    t = node.get("aggregationTemporality")
    return t is None or t in (2, "2", "AGGREGATION_TEMPORALITY_CUMULATIVE")


def _metric_type_and_points(metric: dict) -> tuple[Optional[str], list]:
    """Classify an OTLP metric by instrument type and return its dataPoints.

    A **cumulative** monotonic ``sum`` is a ``"counter"`` — the anomaly layer must
    rate-normalize it (comparing raw cumulative means grows with time). A
    non-monotonic sum, or a *delta*-temporality monotonic sum (already per-interval),
    is a level → ``"sum"``. ``gauge`` is a level. Histograms are **deferred to the
    normalization PR** (ingesting their cumulative ``count`` un-normalized here would
    just feed the saturation #161 flagged), so they yield no samples for now."""
    if "gauge" in metric:
        return "gauge", metric["gauge"].get("dataPoints") or []
    if "sum" in metric:
        s = metric["sum"] or {}
        if s.get("isMonotonic") and _is_cumulative(s):
            return "counter", s.get("dataPoints") or []
        return "sum", s.get("dataPoints") or []
    return None, []


def load_otlp_file(path: Path) -> list[dict]:
    """Load an OTLP-JSON file: JSONL (one object per line, the file exporter's
    default) or a single JSON array/object."""
    path = Path(path)
    if not path.exists():
        return []
    text = path.read_text().strip()
    if not text:
        return []
    lines = [li for li in text.splitlines() if li.strip()]
    if len(lines) > 1 or text[0] != "[":
        objs: list[dict] = []
        for li in lines:
            try:
                objs.append(json.loads(li))
            except json.JSONDecodeError:
                continue
        if objs:
            return objs
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else [parsed]


def capture_from_otlp_dir(otlp_dir: Path):
    """Build a ``generate_incident`` capture callable that reads ``logs.json`` /
    ``traces.json`` / ``metrics.json`` (the Collector's OTLP-JSON export) from a
    directory and window-filters them."""
    otlp_dir = Path(otlp_dir)

    def capture(window_start: datetime, window_end: datetime):
        w = (window_start, window_end)
        logs = parse_otlp_logs(load_otlp_file(otlp_dir / "logs.json"), w)
        spans = parse_otlp_traces(load_otlp_file(otlp_dir / "traces.json"), w)
        metrics = parse_otlp_metrics(load_otlp_file(otlp_dir / "metrics.json"), w)
        return logs, spans, metrics

    return capture
