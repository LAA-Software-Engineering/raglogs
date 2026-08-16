from typing import List, Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

console = Console()


def _parse_params(params: Optional[List[str]]) -> dict:
    result = {}
    for item in params or []:
        key, _, value = item.partition("=")
        if key == "log_groups":
            result[key] = [v.strip() for v in value.split(",") if v.strip()]
        else:
            result[key] = value
    return result


def ingest_cmd(
    paths: Optional[List[str]] = typer.Argument(None, help="Log file paths or directories (supports globs); file adapter only"),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Recurse into directories"),
    source_name: Optional[str] = typer.Option(None, "--source-name", help="Logical source name"),
    service: Optional[str] = typer.Option(None, "--service", help="Default service name"),
    env: Optional[str] = typer.Option(None, "--env", help="Default environment"),
    fmt: str = typer.Option("auto", "--format", help="Log format: json|text|auto"),
    with_embeddings: bool = typer.Option(False, "--with-embeddings/--no-embeddings", help="Generate embeddings"),
    adapter: str = typer.Option("file", "--adapter", help="Source adapter: file|cloudwatch"),
    param: Optional[List[str]] = typer.Option(None, "--param", help="Adapter param as key=value (repeatable)"),
    since: Optional[str] = typer.Option(None, "--since", help="Window, e.g. 30m, 1h, 24h (non-file adapters)"),
    from_time: Optional[str] = typer.Option(None, "--from", help="Window start, ISO 8601 (non-file adapters)"),
    to_time: Optional[str] = typer.Option(None, "--to", help="Window end, ISO 8601 (non-file adapters)"),
    resume_job: Optional[str] = typer.Option(None, "--resume-job", help="Prior ingestion job UUID to resume cursors from (non-file adapters)"),
):
    """Ingest logs into the database."""
    from src.db.session import get_db

    console.print("[bold cyan]Ingesting logs...[/bold cyan]")
    if adapter == "file" and paths:
        console.print(f"  Paths: {', '.join(paths[:3])}{'...' if len(paths) > 3 else ''}")
    elif adapter != "file":
        console.print(f"  Adapter: {adapter}")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Processing files...", total=None)

        def on_progress(lines_read: int, parsed: int):
            progress.update(task, description=f"Processed {lines_read:,} lines, {parsed:,} parsed...")

        try:
            with get_db() as db:
                if adapter == "file":
                    from src.core.ingestion.service import ingest_files

                    job, stats = ingest_files(
                        db=db,
                        paths=paths or [],
                        recursive=recursive,
                        source_name=source_name,
                        default_service=service,
                        default_env=env,
                        fmt=fmt,
                        progress_callback=on_progress,
                    )
                else:
                    import uuid
                    from datetime import datetime

                    from src.adapters.base import SourceSpec, TimeWindow
                    from src.core.ingestion.service import ingest_from_source
                    from src.db.models import IngestionJob
                    from src.utils.time import resolve_window

                    window = None
                    if since or from_time or to_time:
                        from_dt = datetime.fromisoformat(from_time) if from_time else None
                        to_dt = datetime.fromisoformat(to_time) if to_time else None
                        w_start, w_end = resolve_window(since=since, from_time=from_dt, to_time=to_dt)
                        window = TimeWindow(start=w_start, end=w_end)

                    resume_cursors = None
                    if resume_job:
                        prior = db.query(IngestionJob).filter(IngestionJob.id == uuid.UUID(resume_job)).first()
                        if prior is None:
                            raise ValueError(f"No ingestion job found with id {resume_job}")
                        resume_cursors = (prior.metadata_json or {}).get("cursors")

                    spec = SourceSpec(
                        adapter=adapter,
                        params=_parse_params(param),
                        service=service,
                        env=env,
                    )
                    job, stats = ingest_from_source(
                        db=db,
                        spec=spec,
                        window=window,
                        source_name=source_name,
                        fmt=fmt,
                        resume_cursors=resume_cursors,
                        progress_callback=on_progress,
                    )
                job_id = str(job.id)  # read inside session before it closes
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)
        except Exception as e:
            console.print(f"[red]Ingestion failed:[/red] {e}")
            raise typer.Exit(1)

    # Summary table
    table = Table(title="Ingestion complete", show_header=False, box=None)
    table.add_column("", style="dim")
    table.add_column("", style="bold")

    table.add_row("Files processed:", str(stats.files_processed))
    table.add_row("Lines read:", f"{stats.lines_read:,}")
    table.add_row("Parsed logs:", f"{stats.parsed_count:,}")
    table.add_row("Skipped/errors:", f"{stats.error_count:,}")
    if stats.services_detected:
        table.add_row("Services detected:", ", ".join(sorted(stats.services_detected)))
    table.add_row("Duration:", f"{stats.duration_seconds:.1f}s")

    console.print()
    console.print(table)

    if stats.parsed_count == 0:
        console.print("\n[yellow]Warning: No logs were parsed. Check the format and file contents.[/yellow]")
    else:
        console.print(f"\n[green]✓[/green] Ingestion job: {job_id}")
        console.print("\n[dim]Run: raglogs explain --since 1h[/dim]")
