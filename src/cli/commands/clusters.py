from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

console = Console()


def clusters_cmd(
    since: Optional[str] = typer.Option(None, "--since", help="Time window e.g. 1h, 30m"),
    from_time: Optional[str] = typer.Option(None, "--from", help="Start time (ISO 8601)"),
    to_time: Optional[str] = typer.Option(None, "--to", help="End time (ISO 8601)"),
    service: Optional[str] = typer.Option(None, "--service", help="Filter by service"),
    env: Optional[str] = typer.Option(None, "--env", help="Filter by environment"),
    top: int = typer.Option(15, "--top", "-n", help="Number of clusters to show"),
    fmt: str = typer.Option("text", "--format", help="Output format: text|json"),
    all_ingestions: bool = typer.Option(False, "--all-ingestions", help="Include all historical ingestion data (not just latest)"),
    ingestion_job: Optional[str] = typer.Option(None, "--ingestion-job", help="Scope to a specific ingestion job UUID"),
    scope: str = typer.Option(
        "default",
        "--scope",
        help="Isolation scope (CLI default: default)",
    ),
):
    """List top log clusters in a time window."""
    import uuid
    from datetime import datetime

    from src.core.clustering.clusterer import run_clustering
    from src.core.explain.summarizer import get_latest_ingestion_job_id
    from src.db.session import get_db
    from src.utils.time import format_window, resolve_window

    try:
        from_dt = datetime.fromisoformat(from_time) if from_time else None
        to_dt = datetime.fromisoformat(to_time) if to_time else None
        window_start, window_end = resolve_window(since=since, from_time=from_dt, to_time=to_dt)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    with console.status("[cyan]Clustering logs...[/cyan]"):
        try:
            with get_db() as db:
                job_id = None
                if ingestion_job:
                    job_id = uuid.UUID(ingestion_job)
                elif not all_ingestions:
                    job_id = get_latest_ingestion_job_id(db, scope=scope)
                _, clusters = run_clustering(
                    db=db,
                    window_start=window_start,
                    window_end=window_end,
                    service=service,
                    environment=env,
                    max_clusters=top,
                    save_to_db=False,
                    ingestion_job_id=job_id,
                    scope=scope,
                )
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    if fmt == "json":
        import json
        output = {
            "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
            "clusters": [
                {
                    "fingerprint": c.fingerprint,
                    "message": c.representative_message,
                    "count": c.count,
                    "services": list(c.services.keys()),
                    "levels": c.levels,
                    "first_seen": c.first_seen.isoformat() if c.first_seen else None,
                    "last_seen": c.last_seen.isoformat() if c.last_seen else None,
                    "baseline_count": c.baseline_count,
                    "change_ratio": round(c.change_ratio, 2),
                    "importance_score": round(c.importance_score, 2),
                    "is_trigger": c.is_trigger,
                    "merged_fingerprints": c.merged_fingerprints,
                }
                for c in clusters
            ],
        }
        console.print_json(json.dumps(output, default=str))
        return

    console.print(f"\n[bold]Top clusters[/bold] — {format_window(window_start, window_end)}")
    console.print(f"[dim]{len(clusters)} clusters found[/dim]\n")

    if not clusters:
        console.print("[dim]No clusters found in this window.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan", expand=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Count", width=7, justify="right")
    table.add_column("Chg", width=6, justify="right")
    table.add_column("Level", width=7)
    table.add_column("Service(s)", width=20)
    table.add_column("Message", min_width=40)
    table.add_column("First seen", width=9)

    for i, c in enumerate(clusters, 1):
        # Dominant level
        dominant_level = max(c.levels.items(), key=lambda x: x[1])[0] if c.levels else "?"
        level_style = {
            "fatal": "bold red",
            "error": "red",
            "warn": "yellow",
            "info": "green",
            "debug": "dim",
        }.get(dominant_level, "white")

        services_str = ", ".join(list(c.services.keys())[:2])
        if len(c.services) > 2:
            services_str += f" +{len(c.services) - 2}"

        change_str = f"{c.change_ratio:.0f}x" if c.change_ratio >= 2 else f"{c.change_ratio:.1f}x"
        change_style = "bold red" if c.change_ratio >= 10 else ("yellow" if c.change_ratio >= 3 else "dim")

        first_seen_str = c.first_seen.strftime("%H:%M:%S") if c.first_seen else "?"
        trigger_marker = " [bold yellow]⚡[/bold yellow]" if c.is_trigger else ""

        table.add_row(
            str(i),
            str(c.count),
            f"[{change_style}]{change_str}[/{change_style}]",
            f"[{level_style}]{dominant_level}[/{level_style}]",
            services_str,
            c.representative_message[:80] + ("…" if len(c.representative_message or "") > 80 else "") + trigger_marker,
            first_seen_str,
        )

    console.print(table)
    console.print("\n[dim]⚡ = likely trigger event  Chg = change vs baseline[/dim]")
