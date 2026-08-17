"""raglogs worker — background job processor."""

import typer
from rich.console import Console

console = Console()


def worker_cmd(
    poll_interval: int = typer.Option(
        2, "--poll-interval", help="Seconds between idle polls"
    ),
):
    """
    Start the background worker. Processes ingestion jobs enqueued via the API
    and scheduled retention purge jobs (G13).

    Safe to run multiple workers in parallel — uses SELECT FOR UPDATE SKIP LOCKED.
    Graceful shutdown on SIGINT / SIGTERM (Ctrl-C).
    """
    from src.worker.runner import run_worker

    console.print("[dim]raglogs worker starting — Ctrl-C to stop[/dim]")
    try:
        run_worker(poll_interval=poll_interval)
    except SystemExit:
        pass
    console.print("[dim]worker stopped[/dim]")
