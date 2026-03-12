"""
raglogs timeline — reconstruct the sequence of events in an incident window.

Output example:

  Incident timeline  2026-03-12 14:07 → 14:15 UTC

  14:07:26  deploy      Deploy completed for billing-worker v2.4.1
  14:07:27  startup     billing-worker started on port 8080

  14:09:26  error ↑     Stripe signature verification failed (/webhooks/stripe)
                        184 events · billing-worker · 6 min span

  14:09:29  effect      POST /api/checkout 500 (upstream billing error)
                        39 events · api

  14:09:31  effect      Checkout latency increased
                        25 events · api

  14:09:35  symptom     Webhook queue grew to 168 pending items
                        2 events · billing-worker
"""
from typing import Optional

import typer
from rich.console import Console
from rich.text import Text
from rich import box
from rich.table import Table

console = Console()

# Colour palette per category
CATEGORY_STYLE = {
    "deploy":  "bold cyan",
    "startup": "cyan",
    "trigger": "bold yellow",
    "error":   "bold red",
    "effect":  "yellow",
    "symptom": "dim yellow",
}


def timeline_cmd(
    since: Optional[str] = typer.Option(None, "--since", help="Time window e.g. 30m, 1h, 24h"),
    from_time: Optional[str] = typer.Option(None, "--from", help="Start time (ISO 8601)"),
    to_time: Optional[str] = typer.Option(None, "--to", help="End time (ISO 8601)"),
    service: Optional[str] = typer.Option(None, "--service", help="Filter by service"),
    env: Optional[str] = typer.Option(None, "--env", help="Filter by environment"),
    all_ingestions: bool = typer.Option(False, "--all-ingestions", help="Include all ingestion data"),
    ingestion_job: Optional[str] = typer.Option(None, "--ingestion-job", help="Scope to specific ingestion job UUID"),
    fmt: str = typer.Option("text", "--format", help="Output format: text|json"),
):
    """Reconstruct the sequence of events in an incident window."""
    import uuid
    from datetime import datetime

    from src.core.clustering.clusterer import run_clustering
    from src.core.explain.evidence import assemble_evidence
    from src.core.explain.summarizer import get_latest_ingestion_job_id
    from src.core.timeline.builder import build_timeline
    from src.db.session import get_db
    from src.utils.time import format_window, resolve_window

    try:
        from_dt = datetime.fromisoformat(from_time) if from_time else None
        to_dt = datetime.fromisoformat(to_time) if to_time else None
        window_start, window_end = resolve_window(since=since, from_time=from_dt, to_time=to_dt)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    with console.status("[cyan]Reconstructing timeline...[/cyan]"):
        try:
            with get_db() as db:
                job_id = None
                if ingestion_job:
                    job_id = uuid.UUID(ingestion_job)
                elif not all_ingestions:
                    job_id = get_latest_ingestion_job_id(db)

                _, clusters = run_clustering(
                    db=db,
                    window_start=window_start,
                    window_end=window_end,
                    service=service,
                    environment=env,
                    save_to_db=False,
                    ingestion_job_id=job_id,
                )

                packet = assemble_evidence(
                    db=db,
                    window_start=window_start,
                    window_end=window_end,
                    clusters=clusters,
                    service_filter=service,
                    environment_filter=env,
                    ingestion_job_id=job_id,
                )

                events = build_timeline(packet)

        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    if fmt == "json":
        import json
        output = [
            {
                "timestamp": e.timestamp.isoformat(),
                "category": e.category,
                "description": e.description,
                "count": e.count,
                "services": e.services,
                "duration_minutes": e.duration_minutes,
            }
            for e in events
        ]
        console.print_json(json.dumps(output, default=str))
        return

    _render_text(events, window_start, window_end)


def _render_text(events, window_start, window_end):
    from src.utils.time import format_window

    console.print()
    console.print(
        f"[bold]Incident timeline[/bold]  "
        f"[dim]{format_window(window_start, window_end)}[/dim]"
    )
    console.print()

    if not events:
        console.print("[dim]  No significant events found in this window.[/dim]")
        console.print()
        return

    # Group events: insert a blank line when timestamp gap > 1 minute
    prev_ts = None
    for event in events:
        # Blank separator on time gap > 60s
        if prev_ts is not None:
            gap = (event.timestamp - prev_ts).total_seconds()
            if gap > 60:
                console.print()

        ts_str = event.timestamp.strftime("%H:%M:%S")
        label = event.label
        style = CATEGORY_STYLE.get(event.category, "white")

        # Point-in-time events (no count): append service inline
        if event.count is None:
            svc = " · ".join(event.services)
            suffix = f" [dim]· {svc}[/dim]" if svc else ""
            console.print(
                f"  [dim]{ts_str}[/dim]  "
                f"[{style}]{label:<10}[/{style}] "
                f"{event.description}{suffix}"
            )
        else:
            # Volumetric events: main line + sub-line with count · service · duration
            console.print(
                f"  [dim]{ts_str}[/dim]  "
                f"[{style}]{label:<10}[/{style}] "
                f"{event.description}"
            )
            parts = []
            plural = "s" if event.count != 1 else ""
            parts.append(f"{event.count} event{plural}")
            if event.services:
                parts.append(", ".join(event.services))
            if event.duration_minutes:
                parts.append(f"{event.duration_minutes} min span")
            sub = " · ".join(parts)
            console.print(f"             [dim]{sub}[/dim]")

        prev_ts = event.timestamp

    console.print()
