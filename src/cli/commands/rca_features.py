from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

console = Console()


def rca_features_cmd(
    since: Optional[str] = typer.Option(None, "--since", help="Incident window e.g. 10m, 1h"),
    from_time: Optional[str] = typer.Option(None, "--from", help="Incident start (ISO 8601)"),
    to_time: Optional[str] = typer.Option(None, "--to", help="Incident end (ISO 8601)"),
    baseline: str = typer.Option("5m", "--baseline", help="Pre-incident baseline duration e.g. 5m, 300s"),
    scope: str = typer.Option("default", "--scope", help="Isolation scope (default: default)"),
    fmt: str = typer.Option("text", "--format", help="Output format: text|json"),
):
    """Dump the multi-modal RCA feature table for a scope + window (debug).

    Reads persisted logs/traces/metrics and shows the per-service features the
    ranker (C2) will consume — read-only, no effect on explain/RCA output.
    """
    from src.core.rca.features import FEATURE_NAMES, compute_features
    from src.db.session import get_db
    from src.utils.time import format_window, parse_duration, parse_iso, resolve_window

    try:
        from_dt = parse_iso(from_time) if from_time else None
        to_dt = parse_iso(to_time) if to_time else None
        incident_start, incident_end = resolve_window(since=since, from_time=from_dt, to_time=to_dt)
        baseline_start = incident_start - parse_duration(baseline)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    try:
        with get_db() as db:
            table = compute_features(
                db,
                scope,
                incident_start=incident_start,
                incident_end=incident_end,
                baseline_start=baseline_start,
            )
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if fmt == "json":
        import json

        console.print_json(
            json.dumps(
                {
                    "scope": scope,
                    "window": {
                        "baseline_start": baseline_start.isoformat(),
                        "incident_start": incident_start.isoformat(),
                        "incident_end": incident_end.isoformat(),
                    },
                    "present": {
                        "logs": table.has_logs,
                        "traces": table.has_traces,
                        "metrics": table.has_metrics,
                    },
                    "services": [s.as_dict() for s in table.services],
                }
            )
        )
        return

    console.print(f"\n[bold]RCA features[/bold] — {format_window(incident_start, incident_end)}  scope={scope}")
    present = ", ".join(
        m for m, on in (("logs", table.has_logs), ("traces", table.has_traces), ("metrics", table.has_metrics)) if on
    ) or "none"
    console.print(f"[dim]baseline {baseline} · modalities present: {present} · {len(table.services)} candidate services[/dim]\n")

    if not table.services:
        console.print("[dim]No candidate services in this window/scope.[/dim]")
        return

    t = Table(show_header=True, header_style="bold cyan", expand=True)
    t.add_column("Service", min_width=16)
    for name in FEATURE_NAMES:
        t.add_column(name, justify="right")
    for s in sorted(table.services, key=lambda x: (x.met_anom, x.log_grp), reverse=True):
        t.add_row(
            s.service,
            str(s.log_err),
            str(s.log_grp),
            str(s.log_stack),
            f"{s.tr_rate:.2f}",
            f"{s.tr_dur:.2f}",
            f"{s.met_anom:.2f}",
            str(s.has_logs),
            str(s.has_traces),
            str(s.has_metrics),
        )
    console.print(t)
