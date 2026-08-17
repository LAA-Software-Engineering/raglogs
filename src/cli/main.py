import typer
from rich.console import Console

console = Console()


def _build_app() -> typer.Typer:
    from src.cli.commands.init import init_cmd
    from src.cli.commands.ingest import ingest_cmd
    from src.cli.commands.explain import explain_cmd
    from src.cli.commands.clusters import clusters_cmd
    from src.cli.commands.status import status_cmd
    from src.cli.commands.config_cmd import config_cmd
    from src.cli.commands.ask import app as ask_app
    from src.cli.commands.demo import demo_cmd
    from src.cli.commands.worker import worker_cmd
    from src.cli.commands.timeline import timeline_cmd
    from src.cli.commands.compare import compare_cmd
    from src.cli.commands.keys import app as keys_app
    from src.cli.commands.purge import purge_cmd

    _app = typer.Typer(
        name="raglogs",
        help="Incident explanation tool — ask your logs what happened.",
        no_args_is_help=True,
        add_completion=False,
    )

    _app.command("init")(init_cmd)
    _app.command("ingest")(ingest_cmd)
    _app.command("explain")(explain_cmd)
    _app.command("clusters")(clusters_cmd)
    _app.command("status")(status_cmd)
    _app.command("config")(config_cmd)
    _app.command("demo")(demo_cmd)
    _app.command("worker")(worker_cmd)
    _app.command("timeline")(timeline_cmd)
    _app.command("compare")(compare_cmd)
    _app.command("purge")(purge_cmd)
    _app.add_typer(ask_app, name="ask")
    _app.add_typer(keys_app, name="keys")
    return _app


app = _build_app()


def _configure_cli_logging() -> None:
    """JSON when LOG_FORMAT=json; console on a TTY for readable operator output."""
    import sys

    from src.config import get_settings
    from src.observability import setup_observability

    settings = get_settings()
    fmt = settings.log_format
    if fmt == "json" and sys.stderr.isatty():
        fmt = "console"
    setup_observability(log_format=fmt)


def main() -> None:
    _configure_cli_logging()
    app()


if __name__ == "__main__":
    main()
