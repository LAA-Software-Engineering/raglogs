"""Live structural view (#187, Phase I) — produce the deterministic structural result from a scope's
persisted telemetry, for the **opt-in, experimental** structural explanation.

This wires the validated A–E model (:mod:`~src.core.rca.structural_model`, the same one Phase H2
measured offline) into the product path: it reads ``metric_samples`` + ``trace_spans`` for a
``(scope, window)``, builds availability-honest observables + candidate hypotheses, and runs
``partition → resolve`` (Phase D/E) plus ``rank_classes`` (Phase G). It changes **nothing** about the
ordinary explanation — the caller only attaches the result when the feature is explicitly requested.

Honest by construction: it returns ``None`` (abstains) when no incident telemetry produces a candidate,
and it frequently, and correctly, terminates in ``UNCERTAIN`` — the current single-coordinate model
enumerates candidates rather than sharply eliminating (see H2 / #186). ``UNCERTAIN`` is a valid result;
this never invents a resolution the telemetry does not support.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.rca.outcome import StructuralResult, resolve
from src.core.rca.partition import Partition, partition
from src.core.rca.scoring import ScoredClass, rank_classes
from src.core.rca.structural_model import (
    StructuralInputs,
    build_hypotheses,
    build_observables,
    structural_signals,
)
from src.db.models import MetricSample, TraceSpan


def load_structural_rows(
    db: Session, scope: str, window_end: datetime, baseline_start: datetime
) -> tuple[list[MetricSample], list[TraceSpan]]:
    """The ``metric_samples`` and ``trace_spans`` rows the structural model reads for ``scope`` over
    ``[baseline_start, window_end]``. Shared by the live view and the M3 evaluator (#209), so both read
    exactly the persisted rows — series identity included — that the product path sees."""
    metric_rows = db.execute(
        select(MetricSample).where(
            MetricSample.scope == scope,
            MetricSample.ts >= baseline_start,
            MetricSample.ts <= window_end,
        )
    ).scalars().all()
    # Spans over [baseline_start, window_end]. The baseline half feeds span-derived latency ratios and
    # lets a baseline parent resolve for an incident child; call edges are emitted only for incident
    # children (structural_signals -> call_edges(since=window_start)).
    span_rows = db.execute(
        select(TraceSpan).where(
            TraceSpan.scope == scope,
            TraceSpan.start_time >= baseline_start,
            TraceSpan.start_time <= window_end,
        )
    ).scalars().all()
    return list(metric_rows), list(span_rows)


def resolve_structural(inp: StructuralInputs) -> Optional[tuple[StructuralResult, Partition]]:
    """Hypotheses → observables → ``partition → resolve`` for one set of structural inputs, or ``None``
    when no candidate hypothesis exists (an honest abstention — no structural claim)."""
    hypotheses = build_hypotheses(inp.signals, inp.edges, inp.edge_signals, inp.util_signals)
    if not hypotheses:
        return None
    observations = build_observables(inp.signals, inp.edge_signals, inp.util_signals)
    part = partition(hypotheses, observations)
    return resolve(part, observations=observations), part


def build_structural_view(
    db: Session,
    scope: str,
    window_start: datetime,
    window_end: datetime,
    baseline_start: datetime,
) -> Optional[tuple[StructuralResult, tuple[ScoredClass, ...]]]:
    """Build the deterministic structural result + Phase G ranking for ``scope`` over the incident
    window ``[window_start, window_end]`` against the baseline from ``baseline_start``. Returns ``None``
    when the telemetry yields no candidate hypothesis (an honest abstention — no structural claim).

    The ``MetricSample`` / ``TraceSpan`` ORM rows duck-type directly into the shared structural-model
    builders (``service`` / ``value`` / ``ts`` / ``metric`` and ``trace_id`` / ``span_id`` /
    ``parent_span_id`` / ``service``), so the live view runs the exact same model as the shadow eval."""
    metric_rows, span_rows = load_structural_rows(db, scope, window_end, baseline_start)
    # The one shared structural-model builder (#209 M1): span-derived sig (latency median + explicit
    # OTLP status only) merged with the metric sig, and the INCIDENT call graph (edges whose child span
    # starts at/after window_start — the baseline half of this load feeds latency ratios, never edges).
    resolved = resolve_structural(structural_signals(span_rows, metric_rows, window_start))
    if resolved is None:
        return None  # no candidate -> abstain, never a fabricated structural result
    result, part = resolved
    return result, rank_classes(part)
