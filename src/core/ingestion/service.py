import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from src.adapters.base import SourceSpec, TimeWindow
from src.adapters.file.adapter import detect_format, discover_files, read_lines
from src.core.embeddings.provider import EmbeddingsProvider
from src.core.embeddings.store import ingest_embeddings_provider, persist_log_embeddings
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
    received_at: Optional[datetime] = None,
    default_host: Optional[str] = None,
    extra: Optional[dict] = None,
) -> Optional[LogEntry]:
    """Parse, fingerprint, and build a LogEntry for one raw line. Returns None for
    blank/unparseable/error lines (stats are updated either way).

    `received_at` is the adapter-reported time the line was emitted (e.g. a CloudWatch
    event timestamp) — used when the line itself has no parseable timestamp field, so
    rows aren't silently dropped from windowed queries (clustering/explain filter on
    LogEntry.timestamp being both non-null and within the window).

    `default_host` is adapter-provided provenance (e.g. a Loki `pod` label) used when
    the parsed line has no host field. Service/environment defaults are already
    threaded via `default_service` / `default_env`."""
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

    if parsed.timestamp is None and received_at is not None:
        parsed.timestamp = received_at

    normalized, fp = fingerprint_message(parsed.message or "")

    resolved_service = parsed.service or default_service
    entry = LogEntry(
        id=uuid.uuid4(),
        source_id=source.id,
        ingestion_job_id=job.id,
        timestamp=parsed.timestamp,
        service=resolved_service,
        environment=parsed.environment or default_env,
        level=parsed.level,
        trace_id=parsed.trace_id,
        request_id=parsed.request_id,
        host=parsed.host or default_host,
        raw_message=parsed.raw_line[:4096] if parsed.raw_line else None,
        normalized_message=normalized[:2048] if normalized else None,
        fingerprint=fp,
        parser_type=parsed.parser_type,
        extra_json={**(extra or {}), **(parsed.extra or {})} or None,
        source_adapter=source_adapter,
        source_ref=source_ref,
    )
    stats.parsed_count += 1

    if resolved_service:
        stats.services_detected.add(resolved_service)

    return entry


def _flush_log_batch(
    db: Session,
    batch: list[LogEntry],
    embedder: Optional[EmbeddingsProvider],
) -> None:
    """Persist a batch of log entries, then optionally their embeddings."""
    if not batch:
        return
    db.bulk_save_objects(batch)
    db.flush()
    if embedder is not None:
        persist_log_embeddings(db, batch, provider=embedder)
    batch.clear()


def ingest_files(
    db: Session,
    paths: list[str],
    recursive: bool = False,
    source_name: Optional[str] = None,
    default_service: Optional[str] = None,
    default_env: Optional[str] = None,
    fmt: str = "auto",
    progress_callback: Optional[Callable[[int, int], None]] = None,
    with_embeddings: bool = False,
) -> tuple[IngestionJob, IngestionStats]:
    """
    Main ingestion entry point for local files. Discovers, parses, and persists log files.
    Returns the completed IngestionJob and stats.

    When ``with_embeddings`` is True and an embeddings provider is available,
    each flushed batch is also written to ``log_embeddings``. Provider errors
    are skipped so ingest still succeeds.
    """
    import time

    start_time = time.time()
    stats = IngestionStats()
    embedder = ingest_embeddings_provider(with_embeddings)

    # Discover files
    files = discover_files(paths, recursive=recursive)
    stats.files_processed = len(files)

    if not files:
        raise ValueError(f"No files found for paths: {paths}")

    # Create or find source
    source = _get_or_create_source(
        db, name=source_name or ", ".join(paths[:2]), type_="file"
    )

    # Create ingestion job
    job = IngestionJob(
        id=uuid.uuid4(),
        source_id=source.id,
        status="running",
        started_at=datetime.now(tz=timezone.utc),
        file_count=len(files),
        metadata_json={"paths": paths},
        source_adapter="file",
        source_ref=", ".join(str(p) for p in files[:5]),
        mode="batch",
    )
    db.add(job)
    db.flush()

    batch: list[LogEntry] = []

    # NOTE: this try/except marks `job` "failed" on any exception, but that write is
    # only durable if the caller's session commits despite the re-raise below (the
    # worker does — it catches this in process_one() and commits normally). A caller
    # using db.session's get_db()-style context manager that rolls back on exception
    # will discard this flush along with everything else in the transaction, so the
    # job row won't persist at all rather than being left "failed". Both outcomes are
    # fine (no job stuck at "running" either way) — don't "simplify" by dropping the
    # re-raise, it's what lets get_db() know to roll back for those callers.
    try:
        for file_path in files:
            file_fmt = detect_format(file_path, hint=fmt)
            file_service = default_service or _infer_service_from_filename(file_path)

            for line in read_lines(file_path):
                entry = _process_line(
                    line,
                    file_fmt,
                    file_service,
                    default_env,
                    source,
                    job,
                    "file",
                    str(file_path),
                    stats,
                )
                if entry is None:
                    continue

                batch.append(entry)

                if len(batch) >= BATCH_SIZE:
                    _flush_log_batch(db, batch, embedder)

                    if progress_callback:
                        progress_callback(stats.lines_read, stats.parsed_count)

        if batch:
            _flush_log_batch(db, batch, embedder)

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
    resume_cursors: Optional[dict[str, Optional[str]]] = None,
    resume_completed_streams: Optional[Iterable[str]] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    with_embeddings: bool = False,
    existing_job: Optional[IngestionJob] = None,
    finalize: bool = True,
) -> tuple[IngestionJob, IngestionStats]:
    """
    Adapter-driven ingestion entry point (e.g. CloudWatch, Loki). Discovers streams via the
    configured SourceAdapter, reads raw lines within `window`, and persists them through
    the same parse -> fingerprint -> LogEntry pipeline as ingest_files.
    When ``with_embeddings`` is True, flushed batches are also embedded into
    ``log_embeddings`` (fail-open on provider errors).

    Streams that raise AdapterUnavailableError mid-read are skipped (their LogStreamRef
    cursor is saved on the job for a resumable retry) rather than aborting the whole run,
    unless every stream fails before a single line is ingested — in that case the job is
    marked failed with no partial silent ingest, per the ADAPTER_UNAVAILABLE contract.

    `resume_cursors` (keyed by LogStreamRef.stream_id, e.g. a prior job's
    metadata_json["cursors"]) is applied to freshly-discovered refs before reading, so a
    retry can pick up from where a previous partial run left off instead of re-reading
    the whole window. `resume_completed_streams` (a prior job's
    metadata_json["completed_streams"]) is a separate set: those streams are skipped
    entirely rather than re-read from the start of the window — a completed stream's
    saved cursor is None (exhausted), and applying that as a "resume from" token would
    otherwise restart it and duplicate every row it already produced.

    ``existing_job`` appends lines onto a long-lived job (tail mode) instead of
    creating a new IngestionJob. When ``finalize`` is False the job is left
    running: status is not set to completed/failed, streams are not marked
    completed, and counts are additive. Tail ticks pass both.
    """
    import time

    from src.adapters.registry import get_adapter
    from src.config import get_settings
    from src.utils.time import resolve_window

    start_time = time.time()
    stats = IngestionStats()
    embedder = ingest_embeddings_provider(with_embeddings)

    adapter = get_adapter(spec.adapter, get_settings())

    if window is None:
        # Pull adapters query a remote API — default to a bounded recent window rather
        # than epoch-to-now, which would be slow/costly and isn't what any caller wants.
        start, end = resolve_window(since="1h")
        window = TimeWindow(start=start, end=end)

    refs = list(adapter.discover(spec))
    stats.files_processed = len(refs)

    if not refs:
        raise ValueError(
            f"No streams discovered for adapter={spec.adapter!r} params={spec.params!r}"
        )

    completed_at_start = set(resume_completed_streams or ())

    if resume_cursors:
        for ref in refs:
            if (
                ref.stream_id in resume_cursors
                and ref.stream_id not in completed_at_start
            ):
                ref.cursor = resume_cursors[ref.stream_id]

    if existing_job is not None:
        job = existing_job
        source = db.query(Source).filter(Source.id == job.source_id).first()
        if source is None:
            source = _get_or_create_source(
                db,
                name=source_name or ", ".join(r.stream_id for r in refs[:2]),
                type_=spec.adapter,
            )
            job.source_id = source.id
        if not job.source_ref:
            job.source_ref = ", ".join(r.stream_id for r in refs[:5])
        job.file_count = max(job.file_count or 0, len(refs))
    else:
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
            source_ref=", ".join(r.stream_id for r in refs[:5]),
            mode="batch",
        )
        db.add(job)
        db.flush()

    batch: list[LogEntry] = []
    adapter_errors: list[str] = []
    cursors: dict[str, Optional[str]] = {}
    completed_streams: set[str] = set(completed_at_start)

    # See the matching NOTE in ingest_files() re: this try/except and get_db() rollback.
    try:
        for ref in refs:
            if ref.stream_id in completed_at_start:
                continue  # already fully ingested in a prior run — don't duplicate it

            try:
                for raw in adapter.read(ref, window):
                    effective_fmt = _resolve_fmt(raw.text, fmt)
                    entry = _process_line(
                        raw.text,
                        effective_fmt,
                        spec.service or raw.default_service,
                        spec.env or raw.default_environment,
                        source,
                        job,
                        spec.adapter,
                        raw.source_ref,
                        stats,
                        received_at=raw.received_at,
                        default_host=raw.default_host,
                        extra=raw.extra or None,
                    )
                    if entry is None:
                        continue

                    batch.append(entry)

                    if len(batch) >= BATCH_SIZE:
                        _flush_log_batch(db, batch, embedder)

                        if progress_callback:
                            progress_callback(stats.lines_read, stats.parsed_count)
            except AdapterUnavailableError as e:
                adapter_errors.append(str(e))
            else:
                # Tail ticks re-read the same streams forever; cursor=None means
                # "caught up for this window", not "never read again".
                if finalize and ref.cursor is None:
                    completed_streams.add(ref.stream_id)
            finally:
                cursors[ref.stream_id] = ref.cursor

        if adapter_errors and stats.lines_read == 0:
            # every stream failed before yielding anything — no partial silent ingest
            raise AdapterUnavailableError("; ".join(adapter_errors))

        if batch:
            _flush_log_batch(db, batch, embedder)

        stats.duration_seconds = time.time() - start_time
        _apply_ingest_counts(job, stats, additive=existing_job is not None)
        _store_job_cursors(
            job,
            cursors,
            completed_streams,
            adapter_errors,
            finalize=finalize,
        )
        if finalize:
            job.status = "completed"
            job.finished_at = datetime.now(tz=timezone.utc)
        db.flush()
    except Exception as exc:
        if finalize:
            job.status = "failed"
            job.error_message = str(exc)
            job.finished_at = datetime.now(tz=timezone.utc)
            _apply_ingest_counts(job, stats, additive=existing_job is not None)
            _store_job_cursors(
                job,
                cursors,
                completed_streams,
                adapter_errors,
                finalize=True,
            )
            db.flush()
        raise

    return job, stats


def ingest_push_lines(
    db: Session,
    raw_lines: list[str],
    source_name: Optional[str] = None,
    default_service: Optional[str] = None,
    default_env: Optional[str] = None,
    fmt: str = "auto",
    with_embeddings: bool = False,
) -> tuple[IngestionJob, IngestionStats]:
    """Persist caller-pushed raw lines through parse → fingerprint → LogEntry."""
    import time

    start_time = time.time()
    stats = IngestionStats()
    embedder = ingest_embeddings_provider(with_embeddings)

    source = _get_or_create_source(db, name=source_name or "push", type_="push")
    job = IngestionJob(
        id=uuid.uuid4(),
        source_id=source.id,
        status="running",
        started_at=datetime.now(tz=timezone.utc),
        file_count=0,
        metadata_json={"source": "push"},
        source_adapter="push",
        source_ref="push",
        mode="push",
    )
    db.add(job)
    db.flush()

    batch: list[LogEntry] = []
    try:
        for line in raw_lines:
            effective_fmt = _resolve_fmt(line, fmt)
            entry = _process_line(
                line,
                effective_fmt,
                default_service,
                default_env,
                source,
                job,
                "push",
                "push",
                stats,
            )
            if entry is None:
                continue
            batch.append(entry)
            if len(batch) >= BATCH_SIZE:
                _flush_log_batch(db, batch, embedder)

        if batch:
            _flush_log_batch(db, batch, embedder)

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


def _apply_ingest_counts(
    job: IngestionJob,
    stats: IngestionStats,
    *,
    additive: bool,
) -> None:
    if additive:
        job.line_count = (job.line_count or 0) + stats.lines_read
        job.error_count = (job.error_count or 0) + stats.error_count
        job.parsed_count = (job.parsed_count or 0) + stats.parsed_count
    else:
        job.line_count = stats.lines_read
        job.error_count = stats.error_count
        job.parsed_count = stats.parsed_count


def _store_job_cursors(
    job: IngestionJob,
    cursors: dict[str, Optional[str]],
    completed_streams: set[str],
    adapter_errors: list[str],
    *,
    finalize: bool,
) -> None:
    import json

    job.cursor = json.dumps(cursors)
    meta = dict(job.metadata_json or {})
    meta["cursors"] = cursors
    if finalize:
        meta["completed_streams"] = sorted(completed_streams)
        if adapter_errors:
            meta["partial"] = True
    job.metadata_json = meta


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
