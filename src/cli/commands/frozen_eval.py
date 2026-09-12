from pathlib import Path

import typer
from rich.console import Console

console = Console()


def frozen_eval_cmd(
    corpus: Path = typer.Argument(..., help="Eval-case corpus directory (e.g. the OTel-Demo cases)"),
    ranker: str = typer.Option(..., "--ranker", help="Frozen ranker artifact (trained on RCAEval)"),
    calibrator: str = typer.Option(..., "--calibrator", help="Frozen calibrator artifact"),
    fmt: str = typer.Option("text", "--format", help="Output format: text|json"),
    gate: bool = typer.Option(
        True, "--gate/--no-gate",
        help="Also evaluate the frozen abstention gate (#79) — component + end-to-end views. "
             "The ranker is always scored on ALL incident windows with the gate off.",
    ),
):
    """Frozen external validation (#79): score an independent corpus with a
    ranker + calibrator trained ONLY on RCAEval and never touched afterwards.

    Freeze the artifacts before this run — retuning after seeing the corpus turns
    external validation into training on a third corpus.
    """
    import os

    from src.config import get_settings, reload_settings

    # Configure the frozen artifacts for the explain pipeline. Note: the ranker runs
    # with the abstention gate OFF (default), so it is scored on every incident
    # window; the gate decision is computed separately below (a good gate must not
    # be able to hide bad ranker cases by abstaining on them).
    os.environ["RCA_RANKER_MODEL_PATH"] = ranker
    os.environ["RCA_CALIBRATOR_MODEL_PATH"] = calibrator
    reload_settings()
    settings = get_settings()

    from src.core.explain.summarizer import explain_window
    from src.core.ingestion.service import ingest_files
    from src.core.rca.abstention import should_abstain
    from src.core.rca.features import compute_window_anomaly
    from src.db.session import get_db
    from src.eval.case import load_cases
    from src.eval.frozen import frozen_case_result, render_frozen_report, score_frozen
    from src.eval.runner import _ingest_telemetry, _scope_for
    from src.utils.time import parse_duration

    try:
        cases = load_cases(corpus)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    if not cases:
        console.print(f"[red]Error:[/red] no cases in {corpus}")
        raise typer.Exit(1)

    results = []
    try:
        with get_db() as db:
            for case in cases:
                scope = _scope_for(case)
                job, _stats = ingest_files(
                    db=db, paths=[str(p) for p in case.logs_paths], recursive=True, scope=scope
                )
                _ingest_telemetry(db, case, scope, job.id)
                result = explain_window(
                    db=db,
                    window_start=case.window_start,
                    window_end=case.window_end,
                    ingestion_job_id=job.id,
                    scope=scope,
                    no_llm=True,
                    baseline_window_str=case.baseline_window,
                )
                abstained = None
                if gate:
                    anomaly = compute_window_anomaly(
                        db, scope,
                        incident_start=case.window_start,
                        incident_end=case.window_end,
                        baseline_start=case.window_start - parse_duration(
                            case.baseline_window or settings.default_baseline_window
                        ),
                        tau_log=settings.abstention_tau_log,
                        tau_metric=settings.abstention_tau_metric,
                        ingestion_job_id=job.id,
                    )
                    abstained = should_abstain(anomaly, settings.abstention_threshold)
                results.append(frozen_case_result(case, result, abstained))
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    report = score_frozen(results)
    if fmt == "json":
        import json

        console.print_json(json.dumps({
            "provenance": {"mode": "frozen external validation", "ranker": ranker, "calibrator": calibrator},
            **report,
        }, default=str))
        return
    console.print(render_frozen_report(report, ranker_path=ranker, calibrator_path=calibrator))
