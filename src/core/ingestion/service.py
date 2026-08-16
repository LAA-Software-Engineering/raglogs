import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import Session

from src.adapters.base import SourceSpec, TimeWindow
from src.adapters.file.adapter import detect_format, discover_files, read_lines
from src.core.errors import AdapterUnavailableError
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


def _resolve_fmt(line: str, fmt: str) -> str:
    """Per-line format sniff for adapters that don't have a file extension to hint from."""
    if fmt != "auto":
        return fmt
    stripped = line.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return "json"
    return "text"


def _process_line(
    line: str,
    fmt: str,
    default_service: Optional[str],
    default_env: Optional[str],
    source: Source,
    job: IngestionJob,
    source_adapter: str,
    source_ref: str,
    stats: IngestionStats,
) -> Optional[LogEntry]:
    """Parse, fingerprint, and build a LogEntry for one raw line. Returns None for
    blank/unparseable/error lines (stats are updated either way)."""
    stats.lines_read += 1

    if not line.strip():
        return None

    try:
        parsed = _parse_line(line, fmt, default_service=default_service)
    except Exception:
        stats.error_count += 1
        return None

    if parsed is None:
        return None

    if parsed.parse_error:
        stats.error_count += 1
        return None

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
        source_adapter=source_adapter,
        source_ref=source_ref,
    )
    stats.parsed_count += 1

    if parsed.service:
        stats.services_detected.add(parsed.service)

    return entry


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
    Main ingestion entry point for local files. Discovers, parses, and persists log files.
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
    source = _get_or_create_source(db, name=source_name or ", ".join(paths[:2]), type_="file")

    # Create ingestion job
    job = IngestionJob(
        id=uuid.uuid4(),
        source_id=source.id,
        status="running",
        started_at=datetime.now(tz=timezone.utc),
        file_count=len(files),
        metadata_json={"paths": paths},
        source_adapter="file",
    )
    db.add(job)
    db.flush()

    batch: list[LogEntry] = []

    try:
        for file_path in files:
            file_fmt = detect_format(file_path, hint=fmt)
            file_service = default_service or _infer_service_from_filename(file_path)

            for line in read_lines(file_path):
                entry = _process_line(
                    line, file_fmt, file_service, default_env,
                    source, job, "file", str(file_path), stats,
                )
                if entry is None:
                    continue

                batch.append(entry)

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
    except Exception as exc:
        job.status = "failed"
        job.error_message = str(exc)
        job.finished_at = datetime.now(tz=timezone.utc)
        job.line_count = stats.lines_read
        job.error_count = stats.error_count
        job.parsed_count = stats.parsed_count
        db.flush()
        raise

    return job, stats


def ingest_from_source(
    db: Session,
    spec: SourceSpec,
    window: Optional[TimeWindow] = None,
    source_name: Optional[str] = None,
    fmt: str = "auto",
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> tuple[IngestionJob, IngestionStats]:
    """
    Adapter-driven ingestion entry point (e.g. CloudWatch). Discovers streams via the
    configured SourceAdapter, reads raw lines within `window`, and persists them through
    the same parse -> fingerprint -> LogEntry pipeline as ingest_files.

    Streams that raise AdapterUnavailableError mid-read are skipped (their LogStreamRef
    cursor is saved on the job for a resumable retry) rather than aborting the whole run,
    unless every stream fails before a single line is ingested — in that case the job is
    marked failed with no partial silent ingest, per the ADAPTER_UNAVAILABLE contract.
    """
    import time

    from src.adapters.registry import get_adapter
    from src.config import get_settings

    start_time = time.time()
    stats = IngestionStats()

    adapter = get_adapter(spec.adapter, get_settings())

    if window is None:
        window = TimeWindow(
            start=datetime(1970, 1, 1, tzinfo=timezone.utc),
            end=datetime.now(tz=timezone.utc),
        )

    refs = list(adapter.discover(spec))
    stats.files_processed = len(refs)

    if not refs:
        raise ValueError(f"No streams discovered for adapter={spec.adapter!r} params={spec.params!r}")

    source = _get_or_create_source(
        db,
        name=source_name or ", ".join(r.stream_id for r in refs[:2]),
        type_=spec.adapter,
    )

    job = IngestionJob(
        id=uuid.uuid4(),
        source_id=source.id,
        status="running",
        started_at=datetime.now(tz=timezone.utc),
        file_count=len(refs),
        metadata_json={"adapter": spec.adapter, "params": spec.params},
        source_adapter=spec.adapter,
    )
    db.add(job)
    db.flush()

    batch: list[LogEntry] = []
    adapter_errors: list[str] = []
    cursors: dict[str, Optional[str]] = {}

    try:
        for ref in refs:
            try:
                for raw in adapter.read(ref, window):
                    effective_fmt = _resolve_fmt(raw.text, fmt)
                    entry = _process_line(
                        raw.text, effective_fmt, spec.service, spec.env,
                        source, job, spec.adapter, raw.source_ref, stats,
                    )
                    if entry is None:
                        continue

                    batch.append(entry)

                    if len(batch) >= BATCH_SIZE:
                        db.bulk_save_objects(batch)
                        db.flush()
                        batch.clear()

                        if progress_callback:
                            progress_callback(stats.lines_read, stats.parsed_count)
            except AdapterUnavailableError as e:
                adapter_errors.append(str(e))
            finally:
                cursors[ref.stream_id] = ref.cursor

        if adapter_errors and stats.lines_read == 0:
            # every stream failed before yielding anything — no partial silent ingest
            raise AdapterUnavailableError("; ".join(adapter_errors))

        if batch:
            db.bulk_save_objects(batch)
            db.flush()

        stats.duration_seconds = time.time() - start_time

        job.status = "completed"
        job.finished_at = datetime.now(tz=timezone.utc)
        job.line_count = stats.lines_read
        job.error_count = stats.error_count
        job.parsed_count = stats.parsed_count
        job.metadata_json = {
            **(job.metadata_json or {}),
            "cursors": cursors,
            **({"partial": True} if adapter_errors else {}),
        }
        db.flush()
    except Exception as exc:
        job.status = "failed"
        job.error_message = str(exc)
        job.finished_at = datetime.now(tz=timezone.utc)
        job.line_count = stats.lines_read
        job.error_count = stats.error_count
        job.parsed_count = stats.parsed_count
        job.metadata_json = {**(job.metadata_json or {}), "cursors": cursors}
        db.flush()
        raise

    return job, stats


def _get_or_create_source(db: Session, name: str, type_: str = "file") -> Source:
    existing = db.query(Source).filter(Source.name == name).first()
    if existing:
        return existing
    source = Source(id=uuid.uuid4(), name=name, type=type_)
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
