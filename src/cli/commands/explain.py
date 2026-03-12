from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()


def explain_cmd(
    since: Optional[str] = typer.Option(None, "--since", help="Time window e.g. 30m, 1h, 24h"),
    from_time: Optional[str] = typer.Option(None, "--from", help="Start time (ISO 8601)"),
    to_time: Optional[str] = typer.Option(None, "--to", help="End time (ISO 8601)"),
    service: Optional[str] = typer.Option(None, "--service", help="Filter by service"),
    env: Optional[str] = typer.Option(None, "--env", help="Filter by environment"),
    max_clusters: int = typer.Option(10, "--max-clusters", help="Max clusters to analyze"),
    baseline_window: Optional[str] = typer.Option(None, "--baseline-window", help="Baseline window e.g. 24h"),
    fmt: str = typer.Option("text", "--format", help="Output format: text|json|markdown"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip LLM, use deterministic templates"),
    all_ingestions: bool = typer.Option(False, "--all-ingestions", help="Include all historical ingestion data (not just latest)"),
    ingestion_job: Optional[str] = typer.Option(None, "--ingestion-job", help="Scope analysis to a specific ingestion job UUID"),
):
    """Explain what happened in a time window. This is the core command."""
    import uuid
    from datetime import datetime

    from src.core.explain.summarizer import explain_window, get_latest_ingestion_job_id
    from src.db.session import get_db
    from src.utils.time import resolve_window

    # Resolve window
    try:
        from_dt = datetime.fromisoformat(from_time) if from_time else None
        to_dt = datetime.fromisoformat(to_time) if to_time else None
        window_start, window_end = resolve_window(since=since, from_time=from_dt, to_time=to_dt)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("[dim]Example: --since 30m  or  --from 2026-03-12T22:00:00Z --to 2026-03-12T22:30:00Z[/dim]")
        raise typer.Exit(1)

    with console.status("[cyan]Analyzing logs...[/cyan]"):
        try:
            with get_db() as db:
                job_id = None
                if ingestion_job:
                    job_id = uuid.UUID(ingestion_job)
                elif not all_ingestions:
                    # Default: scope to latest ingestion job to avoid count inflation
                    job_id = get_latest_ingestion_job_id(db)
                    if job_id:
                        console.print(f"[dim]Scoped to latest ingestion: {job_id}[/dim]")
                result = explain_window(
                    db=db,
                    window_start=window_start,
                    window_end=window_end,
                    service=service,
                    environment=env,
                    no_llm=no_llm,
                    max_clusters=max_clusters,
                    baseline_window_str=baseline_window,
                    ingestion_job_id=job_id,
                )
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    if fmt == "json":
        import json
        output = {
            "window": {
                "start": result.window_start.isoformat(),
                "end": result.window_end.isoformat(),
            },
            "summary": result.summary_text,
            "confidence": result.confidence,
            "mode": result.mode,
            "total_logs": result.total_logs,
            "services_affected": result.services_affected,
            "primary_cluster": result.primary_cluster,
            "secondary_clusters": result.secondary_clusters,
            "trigger_candidates": result.trigger_candidates,
            "evidence": result.evidence_items,
        }
        console.print_json(json.dumps(output, default=str))
    elif fmt == "markdown":
        md = _render_markdown(result)
        console.print(md)
    else:
        mode_label = "[dim](LLM)[/dim]" if result.mode == "llm" else "[dim](rules)[/dim]"
        console.print()
        console.print(Panel(result.summary_text, title=f"[bold cyan]raglogs explain[/bold cyan] {mode_label}", expand=False))
        console.print()


def _render_markdown(result) -> str:
    from src.utils.time import format_window
    lines = [
        "# Incident Summary",
        "",
        f"**Window:** {format_window(result.window_start, result.window_end)}",
        f"**Services:** {', '.join(result.services_affected) or 'N/A'}",
        f"**Confidence:** {result.confidence}",
        f"**Mode:** {result.mode}",
        "",
        "## Summary",
        "",
        result.summary_text,
        "",
        "## Evidence",
        "",
    ]
    for item in result.evidence_items:
        lines.append(f"- {item}")
    return "\n".join(lines)
