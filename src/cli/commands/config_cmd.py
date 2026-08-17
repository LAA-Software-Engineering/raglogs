from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

console = Console()


def config_cmd(
    key: Optional[str] = typer.Argument(None, help="Config key to get/set"),
    value: Optional[str] = typer.Argument(None, help="Value to set"),
):
    """Show or set configuration values."""
    from src.config import get_settings

    settings = get_settings()

    if key is None:
        # Show all
        table = Table(title="raglogs configuration", show_header=True, header_style="bold cyan")
        table.add_column("Key")
        table.add_column("Value")

        config_items = {
            "DB_URL": settings.db_url,
            "LLM_PROVIDER": settings.llm_provider,
            "LLM_MODEL": settings.llm_model,
            "EMBEDDINGS_PROVIDER": settings.embeddings_provider,
            "EMBEDDINGS_MODEL": settings.embeddings_model,
            "CLUSTER_MERGE_SIMILARITY_THRESHOLD": str(settings.cluster_merge_similarity_threshold),
            "CLUSTER_MERGE_MIN_COUNT": str(settings.cluster_merge_min_count),
            "DEFAULT_BASELINE_WINDOW": settings.default_baseline_window,
            "MAX_CLUSTERS_FOR_EXPLAIN": str(settings.max_clusters_for_explain),
            "MAX_EVIDENCE_ITEMS": str(settings.max_evidence_items),
        }

        for k, v in config_items.items():
            # Mask sensitive values
            if "API_KEY" in k and v:
                v = v[:8] + "..." if len(v) > 8 else "***"
            table.add_row(k, str(v))

        console.print(table)
        console.print("\n[dim]Set values in .env or via environment variables.[/dim]")
    else:
        # Get specific value
        attr = key.lower().replace("raglogs_", "")
        val = getattr(settings, attr, None)
        if val is not None:
            console.print(f"{key} = {val}")
        else:
            console.print(f"[red]Unknown config key:[/red] {key}")
