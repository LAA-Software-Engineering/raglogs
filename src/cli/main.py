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
    _app.add_typer(ask_app, name="ask")
    return _app


app = _build_app()


def main():
    app()


if __name__ == "__main__":
    main()
