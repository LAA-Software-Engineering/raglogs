import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

console = Console()


def init_cmd(
    db_url: str = typer.Option(None, "--db-url", help="PostgreSQL connection URL"),
    run_migrations: bool = typer.Option(True, "--migrate/--no-migrate", help="Run DB migrations"),
):
    """Initialize raglogs configuration and database schema."""
    console.print(Panel("[bold cyan]raglogs init[/bold cyan]", expand=False))

    # Check for .env file
    env_path = Path(".env")
    if not env_path.exists():
        example = Path(".env.example")
        if example.exists():
            import shutil
            shutil.copy(example, env_path)
            console.print(f"[green]✓[/green] Created .env from .env.example")
        else:
            env_path.write_text(
                "DB_URL=postgresql+psycopg://postgres:postgres@localhost:5432/raglogs\n"
                "LLM_PROVIDER=disabled\n"
                "EMBEDDINGS_PROVIDER=disabled\n"
            )
            console.print(f"[green]✓[/green] Created default .env")
    else:
        console.print(f"[dim]  .env already exists[/dim]")

    # Override DB URL if provided
    if db_url:
        _set_env_var(env_path, "DB_URL", db_url)

    # Test DB connection
    try:
        from src.config import reload_settings
        if db_url:
            import os
            os.environ["DB_URL"] = db_url
        reload_settings()

        from src.db.session import check_connection
        if check_connection():
            console.print("[green]✓[/green] Database connection successful")
        else:
            console.print("[red]✗[/red] Could not connect to database. Check DB_URL.")
            raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]✗[/red] Database error: {e}")
        raise typer.Exit(1)

    # Run migrations
    if run_migrations:
        try:
            _run_migrations()
            console.print("[green]✓[/green] Database schema initialized")
        except Exception as e:
            console.print(f"[red]✗[/red] Migration error: {e}")
            raise typer.Exit(1)

    console.print("\n[bold green]raglogs is ready.[/bold green]")
    console.print("\nNext steps:")
    console.print("  [cyan]raglogs ingest ./logs/[/cyan]       — ingest log files")
    console.print("  [cyan]raglogs explain --since 30m[/cyan]  — explain recent activity")


def _run_migrations():
    """Run Alembic migrations programmatically."""
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")


def _set_env_var(env_path: Path, key: str, value: str):
    """Set or update a variable in a .env file."""
    content = env_path.read_text() if env_path.exists() else ""
    lines = content.splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n")
