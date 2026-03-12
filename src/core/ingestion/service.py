import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import Session

from src.adapters.file.adapter import detect_format, discover_files, read_lines
from src.core.normalization.fingerprint import fingerprint_message
from src.core.parsing.json_parser import ParsedLogLine, parse_json_line
from src.core.parsing.text_parser import parse_text_line
from src.db.models import IngestionJob, LogEntry, Source


@dataclass
class IngestionStats:
    files_processed: int = 0
    lines_read: int = 0
    parsed_count: int = 0
    error_count: int = 0
    services_detected: set[str] = field(default_factory=set)
    duration_seconds: float = 0.0


BATCH_SIZE = 500


def _parse_line(
    line: str,
    fmt: str,
    default_service: Optional[str] = None,
) -> Optional[ParsedLogLine]:
    if fmt == "json":
        return parse_json_line(line, default_service=default_service)
    else:
        return parse_text_line(line, default_service=default_service)


def ingest_files(
    db: Session,
    paths: list[str],
    recursive: bool = False,
    source_name: Optional[str] = None,
    default_service: Optional[str] = None,
    default_env: Optional[str] = None,
    fmt: str = "auto",
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> tuple[IngestionJob, IngestionStats]:
    """
    Main ingestion entry point. Discovers, parses, and persists log files.
    Returns the completed IngestionJob and stats.
    """
    import time

    start_time = time.time()
    stats = IngestionStats()

    # Discover files
    files = discover_files(paths, recursive=recursive)
    stats.files_processed = len(files)

    if not files:
        raise ValueError(f"No files found for paths: {paths}")

    # Create or find source
    source = _get_or_create_source(db, name=source_name or ", ".join(paths[:2]))

    # Create ingestion job
    job = IngestionJob(
        id=uuid.uuid4(),
        source_id=source.id,
        status="running",
        started_at=datetime.now(tz=timezone.utc),
        file_count=len(files),
        metadata_json={"paths": paths},
    )
    db.add(job)
    db.flush()

    batch: list[LogEntry] = []
    total_lines = 0

    for file_path in files:
        file_fmt = detect_format(file_path, hint=fmt)
        file_service = default_service or _infer_service_from_filename(file_path)

        for line in read_lines(file_path):
            total_lines += 1
            stats.lines_read += 1

            if not line.strip():
                continue

            try:
                parsed = _parse_line(line, file_fmt, default_service=file_service)
            except Exception:
                stats.error_count += 1
                continue

            if parsed is None:
                continue

            if parsed.parse_error:
                stats.error_count += 1
                continue

            normalized, fp = fingerprint_message(parsed.message or "")

            entry = LogEntry(
                id=uuid.uuid4(),
                source_id=source.id,
                ingestion_job_id=job.id,
                timestamp=parsed.timestamp,
                service=parsed.service or default_service,
                environment=parsed.environment or default_env,
                level=parsed.level,
                trace_id=parsed.trace_id,
                request_id=parsed.request_id,
                host=parsed.host,
                raw_message=parsed.raw_line[:4096] if parsed.raw_line else None,
                normalized_message=normalized[:2048] if normalized else None,
                fingerprint=fp,
                parser_type=parsed.parser_type,
                extra_json=parsed.extra or None,
            )
            batch.append(entry)
            stats.parsed_count += 1

            if parsed.service:
                stats.services_detected.add(parsed.service)

            if len(batch) >= BATCH_SIZE:
                db.bulk_save_objects(batch)
                db.flush()
                batch.clear()

                if progress_callback:
                    progress_callback(stats.lines_read, stats.parsed_count)

    if batch:
        db.bulk_save_objects(batch)
        db.flush()

    stats.duration_seconds = time.time() - start_time

    job.status = "completed"
    job.finished_at = datetime.now(tz=timezone.utc)
    job.line_count = stats.lines_read
    job.error_count = stats.error_count
    job.parsed_count = stats.parsed_count

    db.flush()

    return job, stats


def _get_or_create_source(db: Session, name: str) -> Source:
    existing = db.query(Source).filter(Source.name == name).first()
    if existing:
        return existing
    source = Source(id=uuid.uuid4(), name=name, type="file")
    db.add(source)
    db.flush()
    return source


def _infer_service_from_filename(path: Path) -> Optional[str]:
    """Infer service name from filename heuristics."""
    name = path.stem  # filename without extension
    # Remove common suffixes like .log, dates, numbers
    import re
    name = re.sub(r"[-_]?\d{4}[-_]\d{2}[-_]\d{2}.*$", "", name)
    name = re.sub(r"[-_]?\d+$", "", name)
    name = name.strip("-_")
    return name if name else None
