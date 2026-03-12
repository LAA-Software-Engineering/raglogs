import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

console = Console()


def demo_cmd(
    output_dir: str = typer.Option("./sample_data/sample_incident", "--output-dir", help="Where to write sample logs"),
    ingest: bool = typer.Option(True, "--ingest/--no-ingest", help="Ingest after generating"),
    explain: bool = typer.Option(True, "--explain/--no-explain", help="Run explain after ingesting"),
):
    """Generate fresh sample incident data anchored to now and optionally ingest + explain."""

    now = datetime.now(tz=timezone.utc)
    deploy_time = now - timedelta(minutes=55)
    error_start = deploy_time + timedelta(minutes=2)
    window_duration = 50 * 60  # 50 minutes of errors

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold cyan]Generating sample incident data...[/bold cyan]")
    console.print(f"  Deploy:      {deploy_time.strftime('%H:%M:%S')} UTC (55m ago)")
    console.print(f"  Errors from: {error_start.strftime('%H:%M:%S')} UTC (53m ago)")

    # ── deploy.log ────────────────────────────────────────────────────────────
    deploy_lines = []
    t = deploy_time - timedelta(minutes=7)
    for offset_s, level, svc, msg in [
        (0,   "info", "deployment-controller", "Starting deployment for billing-worker version v2.4.1"),
        (30,  "info", "deployment-controller", "Pulling image billing-worker:v2.4.1"),
        (105, "info", "deployment-controller", "Rolling out billing-worker v2.4.1 — 0/3 pods ready"),
        (130, "info", "deployment-controller", "Rolling out billing-worker v2.4.1 — 1/3 pods ready"),
        (160, "info", "deployment-controller", "Rolling out billing-worker v2.4.1 — 2/3 pods ready"),
        (180, "info", "deployment-controller", "Rolling out billing-worker v2.4.1 — 3/3 pods ready"),
        (420, "info", "deployment-controller", "Deploy completed for billing-worker version v2.4.1"),
        (421, "info", "billing-worker",        "Application started billing-worker v2.4.1 on port 8080"),
        (422, "info", "billing-worker",        "Connected to database postgresql://billing-db:5432/billing"),
        (423, "info", "billing-worker",        "Stripe webhook handler initialized for endpoint /webhooks/stripe"),
    ]:
        deploy_lines.append({
            "timestamp": (t + timedelta(seconds=offset_s)).isoformat(),
            "level": level, "service": svc, "message": msg,
        })

    # ── billing-worker.log ────────────────────────────────────────────────────
    billing_lines = []
    billing_lines.append({
        "timestamp": (deploy_time - timedelta(seconds=10)).isoformat(),
        "level": "info", "service": "billing-worker",
        "message": "Processing webhook queue, 0 pending events",
    })
    for i in range(184):
        t2 = error_start + timedelta(seconds=random.randint(0, window_duration))
        billing_lines.append({
            "timestamp": t2.isoformat(), "level": "error", "service": "billing-worker",
            "message": "Stripe signature verification failed for endpoint /webhooks/stripe",
            "error": "SignatureVerificationError",
        })
    for i in range(45):
        t2 = error_start + timedelta(seconds=random.randint(60, window_duration))
        billing_lines.append({
            "timestamp": t2.isoformat(), "level": "warn", "service": "billing-worker",
            "message": f"Webhook retry attempt 1/3 for event evt_{random.randint(100000, 999999)}",
        })
    for i in range(20):
        t2 = error_start + timedelta(seconds=random.randint(300, window_duration))
        billing_lines.append({
            "timestamp": t2.isoformat(), "level": "warn", "service": "billing-worker",
            "message": f"Webhook queue growing, {random.randint(50, 400)} events pending processing",
        })
    billing_lines.sort(key=lambda x: x["timestamp"])

    # ── api.log ───────────────────────────────────────────────────────────────
    api_lines = []
    cascade_start = deploy_time + timedelta(minutes=3, seconds=30)
    for i in range(39):
        t2 = cascade_start + timedelta(seconds=random.randint(0, window_duration - 90))
        api_lines.append({
            "timestamp": t2.isoformat(), "level": "error", "service": "api",
            "message": "POST /api/checkout 500 Internal Server Error — upstream billing error",
        })
    for i in range(25):
        t2 = cascade_start + timedelta(seconds=random.randint(0, window_duration - 90))
        api_lines.append({
            "timestamp": t2.isoformat(), "level": "warn", "service": "api",
            "message": f"POST /api/checkout 200 OK latency={random.randint(3000, 8000)}ms (high latency detected)",
        })
    for i in range(80):
        t2 = deploy_time + timedelta(seconds=random.randint(0, window_duration))
        api_lines.append({
            "timestamp": t2.isoformat(), "level": "info", "service": "api",
            "message": "POST /api/checkout 200 OK latency=115ms",
        })
    api_lines.sort(key=lambda x: x["timestamp"])

    # ── Write files ───────────────────────────────────────────────────────────
    total = 0
    for fname, lines in [
        ("deploy.log", deploy_lines),
        ("billing-worker.log", billing_lines),
        ("api.log", api_lines),
    ]:
        path = out / fname
        with open(path, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        total += len(lines)
        console.print(f"  [dim]Wrote {len(lines):>4} lines → {path}[/dim]")

    console.print(f"[green]✓[/green] Generated {total} log lines\n")

    if not ingest:
        console.print(f"[dim]Run: raglogs ingest {output_dir}[/dim]")
        return

    # ── Ingest ────────────────────────────────────────────────────────────────
    from src.core.ingestion.service import ingest_files
    from src.db.session import get_db

    console.print("[bold cyan]Ingesting...[/bold cyan]")
    with get_db() as db:
        job, stats = ingest_files(db=db, paths=[str(out)])
        job_id = str(job.id)

    console.print(f"[green]✓[/green] Ingested {stats.parsed_count} logs (job: {job_id})\n")

    if not explain:
        console.print("[dim]Run: raglogs explain --since 1h[/dim]")
        return

    # ── Explain ───────────────────────────────────────────────────────────────
    import uuid
    from src.core.explain.summarizer import explain_window
    from src.utils.time import resolve_window
    from rich.panel import Panel

    console.print("[bold cyan]Analyzing...[/bold cyan]")
    window_start, window_end = resolve_window(since="1h")
    with get_db() as db:
        result = explain_window(
            db=db,
            window_start=window_start,
            window_end=window_end,
            ingestion_job_id=uuid.UUID(job_id),
        )

    mode_label = "[dim][/dim]" if result.mode == "llm" else "[dim](rules)[/dim]"
    console.print(Panel(
        result.summary_text,
        title=f"[bold cyan]raglogs explain[/bold cyan] {mode_label}",
        expand=False,
    ))
