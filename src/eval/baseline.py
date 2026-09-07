"""The trivial baseline arm.

Deliberately dumb: **the most frequent error/fatal cluster in the window, no
trigger, no narrative.** Scored on every run alongside raglogs so the report can
show raglogs' *lift* over `GROUP BY fingerprint ORDER BY count`, not just its
absolute score — if the lift is ~0, that is the single most important thing to
learn.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import LogEntry
from src.eval.metrics import Prediction

# Levels that count as a problem for the baseline's "most frequent error".
_ERROR_LEVELS = ("error", "fatal", "critical")


def baseline_from_counts(rows: list[tuple[Optional[str], str, int]]) -> Prediction:
    """Build the baseline prediction from ``(service, fingerprint, count)`` rows.

    Rows must be ordered most-frequent first. Pure, so it is unit-testable
    without a database. The baseline emits an explanation iff any error/fatal
    cluster exists, predicts that cluster's service, never returns a trigger,
    and always reports ``low`` confidence (it has no scoring).
    """
    if not rows:
        return Prediction(produced_explanation=False, confidence="low")
    service, _fingerprint, _count = rows[0]
    services = [service] if service else []
    return Prediction(
        produced_explanation=True,
        root_cause_service=service,
        predicted_services=services,
        top_trigger_timestamp=None,
        returned_any_trigger=False,
        confidence="low",
    )


def baseline_prediction(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    ingestion_job_id: uuid.UUID,
) -> Prediction:
    """Compute the baseline over one job's error/fatal logs in the window."""
    stmt = (
        select(LogEntry.service, LogEntry.fingerprint, func.count().label("n"))
        .where(
            LogEntry.ingestion_job_id == ingestion_job_id,
            LogEntry.timestamp >= window_start,
            LogEntry.timestamp <= window_end,
            func.lower(LogEntry.level).in_(_ERROR_LEVELS),
        )
        .group_by(LogEntry.service, LogEntry.fingerprint)
        .order_by(func.count().desc())
    )
    rows = [(r.service, r.fingerprint, r.n) for r in db.execute(stmt).all()]
    return baseline_from_counts(rows)
