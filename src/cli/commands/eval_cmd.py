from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

console = Console()

_DEFAULT_CASES = "tests/eval/cases"
_DEFAULT_JSON = "eval_results.json"


def eval_cmd(
    cases_dir: str = typer.Option(
        _DEFAULT_CASES, "--cases", help="Directory of eval case subdirectories"
    ),
    json_out: Optional[str] = typer.Option(
        _DEFAULT_JSON, "--json", help="Write the report JSON here (empty to skip)"
    ),
):
    """Run raglogs + a trivial baseline over labeled cases and report accuracy.

    Requires a database (ingests each case, then runs the explain pipeline).
    Reports raglogs' lift over the baseline, not just its absolute score.
    """
    from src.eval.case import load_cases
    from src.eval.report import build_report, render_table, write_json
    from src.eval.runner import run_cases

    cases = load_cases(Path(cases_dir))
    if not cases:
        console.print(f"[yellow]No cases found under {cases_dir}[/yellow]")
        raise typer.Exit(1)

    console.print(f"[bold cyan]Running eval over {len(cases)} cases...[/bold cyan]")

    from src.db.session import get_db

    with get_db() as db:
        results = run_cases(db, cases)

    report = build_report(results)
    console.print()
    console.print(render_table(report))

    if json_out:
        write_json(report, Path(json_out))
        console.print(f"\n[green]✓[/green] Wrote {json_out}")
