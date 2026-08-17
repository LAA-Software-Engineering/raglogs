"""Helpers that pin LogEntry / IngestionJob queries to a single scope.

Every analysis and ingest-list path must go through these so one scope's
rows cannot appear in another's SQL (including baseline comparison).
"""

from __future__ import annotations

from sqlalchemy import Select
from sqlalchemy.sql.elements import ColumnElement

from src.db.models import IngestionJob, LogEntry


def log_entry_scope_clause(scope: str) -> ColumnElement[bool]:
    """``LogEntry.scope == scope`` — use in additional ``where()`` calls."""
    return LogEntry.scope == scope


def ingestion_job_scope_clause(scope: str) -> ColumnElement[bool]:
    """``IngestionJob.scope == scope`` — use in additional ``where()`` calls."""
    return IngestionJob.scope == scope


def filter_log_entries_by_scope(stmt: Select, scope: str) -> Select:
    """Restrict a ``LogEntry`` select to ``scope``."""
    return stmt.where(log_entry_scope_clause(scope))


def filter_ingestion_jobs_by_scope(stmt: Select, scope: str) -> Select:
    """Restrict an ``IngestionJob`` select to ``scope``."""
    return stmt.where(ingestion_job_scope_clause(scope))
