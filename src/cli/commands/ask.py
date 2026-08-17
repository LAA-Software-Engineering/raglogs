from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel

app = typer.Typer()
console = Console()


@app.callback(invoke_without_command=True)
def ask_cmd(
    ctx: typer.Context,
    question: Optional[str] = typer.Argument(None, help="Your question about the logs"),
    since: Optional[str] = typer.Option(None, "--since", help="Time window e.g. 2h"),
    service: Optional[str] = typer.Option(None, "--service", help="Filter by service"),
    fmt: str = typer.Option("text", "--format", help="Output format: text|json"),
):
    """Ask a natural language question about your logs."""
    if ctx.invoked_subcommand is not None:
        return

    if not question:
        console.print("[red]Error:[/red] Please provide a question.")
        console.print("[dim]Example: raglogs ask 'why did login fail?'[/dim]")
        raise typer.Exit(1)

    from src.core.retrieval.question_router import answer_question
    from src.db.session import get_db
    from src.utils.time import resolve_window

    window_start, window_end = None, None
    if since:
        try:
            window_start, window_end = resolve_window(since=since)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    with console.status(f"[cyan]Searching logs for: {question}[/cyan]"):
        try:
            with get_db() as db:
                result = answer_question(
                    db=db,
                    question=question,
                    window_start=window_start,
                    window_end=window_end,
                    service=service,
                )
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    if fmt == "json":
        import json
        output = {
            "question": result.question,
            "answer": result.answer_text,
            "evidence": result.evidence_items,
            "clusters": result.clusters_used,
            "total_matches": result.total_matches,
            "retrieval_mode": result.retrieval_mode,
        }
        console.print_json(json.dumps(output))
    else:
        console.print()
        console.print(Panel(result.answer_text, title="[bold cyan]raglogs ask[/bold cyan]", expand=False))
        console.print(f"[dim]retrieval: {result.retrieval_mode}[/dim]")
        console.print()
