"""Ingestion for telemetry (traces + metrics) — #118 multi-modal RCA.

Parallel to the log ingest path in ``service.py``, but for the additive
``trace_spans`` / ``metric_samples`` tables. Kept source-agnostic: adapters /
converters produce ``ParsedSpan`` / ``ParsedMetricSample`` and these helpers
persist them.

**Idempotency** (review note on #125): these tables have no dedup index, so a
re-ingest would otherwise inflate feature counts. Each row's primary key is a
deterministic ``uuid5`` of its identifying fields, and inserts use
``ON CONFLICT (id) DO NOTHING`` — re-ingesting the same telemetry is a no-op,
without adding a unique index.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.db.models import DEFAULT_LOG_SCOPE, MetricSample, TraceSpan

# Stable namespace so deterministic ids are reproducible across processes.
_NS = uuid.UUID("f1e2d3c4-b5a6-4778-9900-112233445566")

_BATCH = 1000


@dataclass
class ParsedSpan:
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    service: Optional[str] = None
    operation: Optional[str] = None
    start_time: Optional[datetime] = None
    duration_ms: Optional[float] = None
    status_code: Optional[str] = None
    attributes: Optional[dict[str, Any]] = None


@dataclass
class ParsedMetricSample:
    service: Optional[str] = None
    metric: str = ""
    value: Optional[float] = None
    ts: Optional[datetime] = None
    attributes: Optional[dict[str, Any]] = None
    # OTLP instrument type: "gauge" | "counter" (monotonic sum) | "sum" |
    # "histogram". None when the source doesn't carry type (treated as gauge).
    metric_type: Optional[str] = None


def span_pk(scope: str, s: ParsedSpan) -> uuid.UUID:
    """Deterministic id for a span — (scope, trace_id, span_id). Falls back to
    start_time/service when span_id is absent so distinct rows don't collapse."""
    key = f"{scope}|{s.trace_id}|{s.span_id}|{s.start_time}|{s.service}"
    return uuid.uuid5(_NS, key)


def metric_pk(scope: str, m: ParsedMetricSample) -> uuid.UUID:
    """Deterministic id for a metric sample — (scope, service, metric, ts)."""
    key = f"{scope}|{m.service}|{m.metric}|{m.ts}"
    return uuid.uuid5(_NS, key)


def _chunks(rows: list[dict], n: int = _BATCH):
    for i in range(0, len(rows), n):
        yield rows[i : i + n]


def persist_spans(
    db: Session,
    spans: list[ParsedSpan],
    scope: str = DEFAULT_LOG_SCOPE,
    ingestion_job_id: Optional[uuid.UUID] = None,
) -> int:
    """Idempotently persist spans. Returns the number of input rows (deduped by
    deterministic PK on conflict)."""
    scope = scope or DEFAULT_LOG_SCOPE
    rows = [
        {
            "id": span_pk(scope, s),
            "ingestion_job_id": ingestion_job_id,
            "trace_id": s.trace_id,
            "span_id": s.span_id,
            "parent_span_id": s.parent_span_id,
            "service": s.service,
            "operation": s.operation,
            "start_time": s.start_time,
            "duration_ms": s.duration_ms,
            "status_code": s.status_code,
            "attributes": s.attributes,
            "scope": scope,
        }
        for s in spans
    ]
    for chunk in _chunks(rows):
        db.execute(pg_insert(TraceSpan).values(chunk).on_conflict_do_nothing(index_elements=["id"]))
    return len(rows)


def persist_metric_samples(
    db: Session,
    samples: list[ParsedMetricSample],
    scope: str = DEFAULT_LOG_SCOPE,
    ingestion_job_id: Optional[uuid.UUID] = None,
) -> int:
    """Idempotently persist metric samples (deduped by deterministic PK)."""
    scope = scope or DEFAULT_LOG_SCOPE
    rows = [
        {
            "id": metric_pk(scope, m),
            "ingestion_job_id": ingestion_job_id,
            "service": m.service,
            "metric": m.metric,
            "value": m.value,
            "metric_type": m.metric_type,
            "ts": m.ts,
            "attributes": m.attributes,
            "scope": scope,
        }
        for m in samples
    ]
    for chunk in _chunks(rows):
        db.execute(pg_insert(MetricSample).values(chunk).on_conflict_do_nothing(index_elements=["id"]))
    return len(rows)
