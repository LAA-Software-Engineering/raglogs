"""
raglogs compare — diff two time windows by their cluster sets.

Usage examples:

  raglogs compare --since 30m --baseline 24h
      Compare the last 30 minutes against the same 30-minute window 24h ago.

  raglogs compare --since 1h --baseline 7d
      Compare the last hour against the same window from 7 days ago.

  raglogs compare --window-a-from 2026-03-12T14:00:00Z --window-a-to 2026-03-12T14:30:00Z \
                  --window-b-from 2026-03-11T14:00:00Z --window-b-to 2026-03-11T14:30:00Z
      Compare two explicit windows.

Output sections:
  +  New clusters      — appear in A, absent in B
  -  Disappeared       — present in B, gone in A
  ↑  Increased         — in both, count grew by >50%
  ↓  Decreased         — in both, count shrank by >50%
  +⚡ New triggers      — deploy/restart events only in A
"""
from typing import Optional

import typer
from rich.console import Console

console = Console()

SECTION_STYLES = {
    "new":         ("bold green",  "+"),
    "disappeared": ("dim red",     "-"),
    "increased":   ("bold yellow", "↑"),
    "decreased":   ("dim cyan",    "↓"),
    "trigger_new": ("bold cyan",   "+⚡"),
    "trigger_old": ("dim",         "-⚡"),
}


def compare_cmd(
    since: Optional[str] = typer.Option(None, "--since",
        help="Incident window size, e.g. 30m, 1h. Window A ends now."),
    baseline: Optional[str] = typer.Option(None, "--baseline",
        help="How far back window B starts, e.g. 24h, 7d. Same duration as --since."),
    window_a_from: Optional[str] = typer.Option(None, "--window-a-from",
        help="Start of window A (ISO 8601)"),
    window_a_to: Optional[str] = typer.Option(None, "--window-a-to",
        help="End of window A (ISO 8601)"),
    window_b_from: Optional[str] = typer.Option(None, "--window-b-from",
        help="Start of window B (ISO 8601)"),
    window_b_to: Optional[str] = typer.Option(None, "--window-b-to",
        help="End of window B (ISO 8601)"),
    service: Optional[str] = typer.Option(None, "--service",
        help="Filter both windows to one service"),
    env: Optional[str] = typer.Option(None, "--env",
        help="Filter both windows to one environment"),
    all_ingestions: bool = typer.Option(False, "--all-ingestions",
        help="Include all historical ingestion data"),
    fmt: str = typer.Option("text", "--format",
        help="Output format: text|json"),
    scope: str = typer.Option(
        "default",
        "--scope",
        help="Isolation scope (CLI default: default)",
    ),
):
    """Diff two time windows — see exactly what changed."""
    from datetime import datetime, timezone

    from src.core.clustering.clusterer import run_clustering
    from src.core.compare.differ import compare_windows
    from src.core.explain.evidence import assemble_evidence
    from src.core.explain.summarizer import get_latest_ingestion_job_id
    from src.db.session import get_db
    from src.utils.time import parse_duration

    now = datetime.now(tz=timezone.utc)

    # ── Resolve windows ───────────────────────────────────────────────────────
    try:
        if since and baseline:
            # --since 30m --baseline 24h
            # A: last 30m ending now
            # B: same 30m window, offset back by baseline duration
            incident_delta = parse_duration(since)
            baseline_delta = parse_duration(baseline)
            a_end = now
            a_start = now - incident_delta
            b_end = now - baseline_delta
            b_start = b_end - incident_delta

        elif window_a_from and window_a_to and window_b_from and window_b_to:
            # Explicit ISO windows
            a_start = datetime.fromisoformat(window_a_from).replace(tzinfo=timezone.utc)
            a_end   = datetime.fromisoformat(window_a_to).replace(tzinfo=timezone.utc)
            b_start = datetime.fromisoformat(window_b_from).replace(tzinfo=timezone.utc)
            b_end   = datetime.fromisoformat(window_b_to).replace(tzinfo=timezone.utc)

        else:
            console.print("[red]Error:[/red] Provide either --since + --baseline, "
                          "or --window-a-from/to + --window-b-from/to")
            raise typer.Exit(1)

    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    with console.status("[cyan]Comparing windows...[/cyan]"):
        try:
            with get_db() as db:
                job_id = None if all_ingestions else get_latest_ingestion_job_id(db, scope=scope)

                # Cluster both windows independently (no baseline comparison needed)
                _, clusters_a = run_clustering(
                    db=db, window_start=a_start, window_end=a_end,
                    service=service, environment=env,
                    save_to_db=False, ingestion_job_id=job_id,
                    max_clusters=50, scope=scope,
                )
                _, clusters_b = run_clustering(
                    db=db, window_start=b_start, window_end=b_end,
                    service=service, environment=env,
                    save_to_db=False, ingestion_job_id=job_id,
                    max_clusters=50, scope=scope,
                )

                # Get trigger candidates from evidence assembly for both windows
                packet_a = assemble_evidence(
                    db=db, window_start=a_start, window_end=a_end,
                    clusters=clusters_a, service_filter=service,
                    environment_filter=env, ingestion_job_id=job_id,
                    scope=scope,
                )
                packet_b = assemble_evidence(
                    db=db, window_start=b_start, window_end=b_end,
                    clusters=clusters_b, service_filter=service,
                    environment_filter=env, ingestion_job_id=job_id,
                    scope=scope,
                )

                result = compare_windows(
                    clusters_a=clusters_a,
                    clusters_b=clusters_b,
                    triggers_a=packet_a.trigger_candidates,
                    triggers_b=packet_b.trigger_candidates,
                    window_a_start=a_start, window_a_end=a_end,
                    window_b_start=b_start, window_b_end=b_end,
                )

        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    if fmt == "json":
        _render_json(result)
    else:
        _render_text(result)


def _trunc(text: str, n: int = 72) -> str:
    if len(text) <= n:
        return text
    cut = text[:n]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n // 2 else cut) + "…"


def _render_text(result) -> None:
    from src.utils.time import format_window

    console.print()
    console.print("[bold]Incident comparison[/bold]")
    console.print()
    console.print(f"  [dim]Window A (now):[/dim]      {format_window(result.window_a_start, result.window_a_end)}")
    console.print(f"  [dim]Window B (baseline):[/dim] {format_window(result.window_b_start, result.window_b_end)}")
    console.print()

    if not result.has_changes:
        console.print("  [dim]No significant changes between windows.[/dim]")
        console.print()
        return

    def _row(sigil: str, style: str, msg: str, count_a: Optional[int], count_b: Optional[int]):
        count_str = _format_counts(count_a, count_b)
        console.print(
            f"  [{style}]{sigil}[/{style}] "
            f"{_trunc(msg):<74} "
            f"[dim]{count_str}[/dim]"
        )

    # ── New clusters ──────────────────────────────────────────────────────────
    if result.new_clusters:
        console.print("[bold green]New error clusters[/bold green]")
        for d in result.new_clusters:
            _row("+", "bold green", d.message, d.count_a, d.count_b)
        console.print()

    # ── Disappeared ───────────────────────────────────────────────────────────
    if result.disappeared_clusters:
        console.print("[dim red]Errors that disappeared[/dim red]")
        for d in result.disappeared_clusters:
            _row("-", "dim red", d.message, d.count_a, d.count_b)
        console.print()

    # ── Increased ─────────────────────────────────────────────────────────────
    if result.increased_clusters:
        console.print("[bold yellow]Errors that increased[/bold yellow]")
        for d in result.increased_clusters:
            _row("↑", "bold yellow", d.message, d.count_a, d.count_b)
        console.print()

    # ── Decreased ─────────────────────────────────────────────────────────────
    if result.decreased_clusters:
        console.print("[dim cyan]Errors that decreased[/dim cyan]")
        for d in result.decreased_clusters:
            _row("↓", "dim cyan", d.message, d.count_a, d.count_b)
        console.print()

    # ── New triggers ──────────────────────────────────────────────────────────
    if result.new_triggers:
        console.print("[bold cyan]Triggers in A not seen in B[/bold cyan]")
        for t in result.new_triggers:
            svc = f" [dim]· {t.service}[/dim]" if t.service else ""
            console.print(f"  [bold cyan]+⚡[/bold cyan] {_trunc(t.message)}{svc}")
        console.print()

    if result.dropped_triggers:
        console.print("[dim]Triggers in B not seen in A[/dim]")
        for t in result.dropped_triggers:
            svc = f" [dim]· {t.service}[/dim]" if t.service else ""
            console.print(f"  [dim]-⚡ {_trunc(t.message)}{svc}[/dim]")
        console.print()


def _format_counts(count_a: Optional[int], count_b: Optional[int]) -> str:
    if count_a is not None and count_b is None:
        return f"{count_a} events"
    if count_b is not None and count_a is None:
        return f"{count_b} events"
    if count_a is not None and count_b is not None:
        return f"{count_b} → {count_a}"
    return ""


def _render_json(result) -> None:
    import json

    def _diff_to_dict(d):
        return {
            "fingerprint": d.fingerprint,
            "message": d.message,
            "services": d.services,
            "count_a": d.count_a,
            "count_b": d.count_b,
        }

    output = {
        "window_a": {
            "start": result.window_a_start.isoformat(),
            "end": result.window_a_end.isoformat(),
        },
        "window_b": {
            "start": result.window_b_start.isoformat(),
            "end": result.window_b_end.isoformat(),
        },
        "new_clusters":         [_diff_to_dict(d) for d in result.new_clusters],
        "disappeared_clusters": [_diff_to_dict(d) for d in result.disappeared_clusters],
        "increased_clusters":   [_diff_to_dict(d) for d in result.increased_clusters],
        "decreased_clusters":   [_diff_to_dict(d) for d in result.decreased_clusters],
        "new_triggers":         [{"message": t.message, "service": t.service} for t in result.new_triggers],
        "dropped_triggers":     [{"message": t.message, "service": t.service} for t in result.dropped_triggers],
    }
    console.print_json(json.dumps(output, default=str))
