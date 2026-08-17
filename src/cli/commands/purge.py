"""raglogs purge — expire raw logs and (later) cluster summaries."""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

console = Console()


def purge_cmd(
    scope: Optional[str] = typer.Option(
        None,
        "--scope",
        help="Limit to one isolation scope (default: every scope with data)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Count expired rows without deleting them",
    ),
) -> None:
    """Delete expired raw logs, then expired cluster summaries / embeddings.

    Raw rows older than RETENTION_RAW (per-scope override in scope_retention)
    are removed; cluster_embeddings stay so similar-incident search still
    works. After RETENTION_SUMMARY those summaries go too. 0 / empty / off
    skips that tier. The worker also runs this on an idle poll.
    """
    from src.core.retention.purge import run_purge
    from src.db.session import get_db

    with get_db() as db:
        counts = run_purge(
            db,
            scope=scope,
            dry_run=dry_run,
            max_chunks=None,
        )

    table = Table(
        title="raglogs purge" + (" (dry-run)" if dry_run else ""),
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Kind")
    table.add_column("Rows", justify="right")
    table.add_row("raw", f"{counts.raw:,}")
    table.add_row("summary", f"{counts.summary:,}")
    table.add_row("embedding", f"{counts.embedding:,}")
    console.print(table)
    if counts.scopes:
        console.print(f"[dim]scopes:[/dim] {', '.join(counts.scopes)}")
    if dry_run:
        console.print("[dim]No rows were deleted (--dry-run).[/dim]")
