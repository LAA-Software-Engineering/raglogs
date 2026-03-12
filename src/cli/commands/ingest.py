import time
from typing import List, Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

console = Console()


def ingest_cmd(
    paths: List[str] = typer.Argument(..., help="Log file paths or directories (supports globs)"),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Recurse into directories"),
    source_name: Optional[str] = typer.Option(None, "--source-name", help="Logical source name"),
    service: Optional[str] = typer.Option(None, "--service", help="Default service name"),
    env: Optional[str] = typer.Option(None, "--env", help="Default environment"),
    fmt: str = typer.Option("auto", "--format", help="Log format: json|text|auto"),
    with_embeddings: bool = typer.Option(False, "--with-embeddings/--no-embeddings", help="Generate embeddings"),
):
    """Ingest log files into the database."""
    from src.core.ingestion.service import ingest_files
    from src.db.session import get_db

    console.print(f"[bold cyan]Ingesting logs...[/bold cyan]")
    if paths:
        console.print(f"  Paths: {', '.join(paths[:3])}{'...' if len(paths) > 3 else ''}")

    start = time.time()

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
                job, stats = ingest_files(
                    db=db,
                    paths=paths,
                    recursive=recursive,
                    source_name=source_name,
                    default_service=service,
                    default_env=env,
                    fmt=fmt,
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
