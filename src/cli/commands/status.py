import os

import typer
from rich.console import Console
from rich.table import Table

console = Console()


def status_cmd():
    """Show database, job, and provider status."""
    from sqlalchemy import func, select

    from src.config import get_settings
    from src.db.models import IngestionJob, LogEntry, Source
    from src.db.session import check_connection, get_db

    settings = get_settings()

    table = Table(title="raglogs status", show_header=False, box=None)
    table.add_column("", style="dim", width=25)
    table.add_column("")

    # DB connection
    db_ok = check_connection()
    table.add_row("Database:", "[green]connected[/green]" if db_ok else "[red]disconnected[/red]")
    table.add_row("DB URL:", settings.db_url[:60] + "..." if len(settings.db_url) > 60 else settings.db_url)

    # Log count
    if db_ok:
        try:
            with get_db() as db:
                log_count = db.execute(select(func.count(LogEntry.id))).scalar() or 0
                source_count = db.execute(select(func.count(Source.id))).scalar() or 0
                job_count = db.execute(select(func.count(IngestionJob.id))).scalar() or 0
            table.add_row("Log entries:", f"{log_count:,}")
            table.add_row("Sources:", str(source_count))
            table.add_row("Ingestion jobs:", str(job_count))
        except Exception as e:
            table.add_row("Log entries:", f"[red]Error: {e}[/red]")

    openai_key = os.environ.get("OPENAI_API_KEY", "")

    table.add_row("", "")
    table.add_row("LLM provider:", _provider_status(settings.llm_provider, openai_key if settings.llm_provider == "openai" else "n/a"))
    table.add_row("LLM model:", settings.llm_model)
    table.add_row("Embeddings:", _provider_status(settings.embeddings_provider, openai_key if settings.embeddings_provider == "openai" else "n/a"))

    console.print()
    console.print(table)
    console.print()


def _provider_status(provider: str, api_key: str) -> str:
    if provider == "disabled":
        return "[dim]disabled[/dim]"
    if provider == "openai" and not api_key:
        return f"[yellow]{provider} (no API key)[/yellow]"
    return f"[green]{provider}[/green]"
