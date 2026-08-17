"""Admin CLI for minting and revoking HTTP API keys.

Plaintext keys are printed once and never written to logs.
"""

from __future__ import annotations

import uuid
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.api.auth.keys import VALID_ROLES

app = typer.Typer(
    name="keys",
    help="Mint, list, and revoke HTTP API keys. Plaintext is shown only at create time.",
    no_args_is_help=True,
)
console = Console()


@app.command("create")
def create_cmd(
    role: str = typer.Option("query", "--role", help="ingest | query | admin"),
    scope: str = typer.Option(
        "default",
        "--scope",
        help="Pin this key to a scope (enforced on every service read/write). Convention: incident:<id>, service:<name>, env:<name>",
    ),
    allow_scope_override: bool = typer.Option(
        False,
        "--allow-scope-override",
        help="Allow the caller to pass a request scope other than the key's pinned scope",
    ),
    name: Optional[str] = typer.Option(
        None, "--name", help="Optional label for this key"
    ),
) -> None:
    """Create an API key and print the plaintext API key and webhook secret once."""
    from src.api.auth.keys import create_api_key

    role = role.strip().lower()
    if role not in VALID_ROLES:
        console.print(f"[red]Error:[/red] role must be one of {sorted(VALID_ROLES)}")
        raise typer.Exit(1)

    try:
        plaintext, webhook_secret, info = create_api_key(
            role=role,
            scope=scope,
            name=name,
            allow_scope_override=allow_scope_override,
        )
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)

    console.print(
        Panel(
            plaintext,
            title="[bold cyan]API key — copy it now; it will not be shown again[/bold cyan]",
            expand=False,
        )
    )
    console.print(
        Panel(
            webhook_secret,
            title="[bold cyan]Webhook signing secret — copy it now; it will not be shown again[/bold cyan]",
            expand=False,
        )
    )
    console.print(
        f"[dim]id={info.id}  prefix={info.key_prefix}  role={info.role}  "
        f"scope={info.scope}  allow_scope_override={info.allow_scope_override}[/dim]"
    )
    console.print(
        "[dim]HMAC ingest callbacks with the webhook secret (whsec_…), not the API key.[/dim]"
    )


@app.command("list")
def list_cmd() -> None:
    """List keys (id, prefix, role, scope, webhook preview, created, revoked). Hashes are never shown."""
    from src.api.auth.keys import list_api_keys

    try:
        keys = list_api_keys()
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)

    table = Table(title="API keys", show_header=True, header_style="bold cyan")
    table.add_column("id")
    table.add_column("prefix")
    table.add_column("role")
    table.add_column("scope")
    table.add_column("override")
    table.add_column("webhook")
    table.add_column("name")
    table.add_column("created")
    table.add_column("revoked")

    for key in keys:
        created = key.created_at.isoformat() if key.created_at else ""
        revoked = key.revoked_at.isoformat() if key.revoked_at else ""
        table.add_row(
            str(key.id),
            key.key_prefix,
            key.role,
            key.scope,
            "yes" if key.allow_scope_override else "no",
            key.webhook_secret_preview or "",
            key.name or "",
            created,
            revoked,
        )

    console.print(table)
    if not keys:
        console.print(
            "[dim]No API keys. Create one with: raglogs keys create --role query[/dim]"
        )


@app.command("revoke")
def revoke_cmd(
    key_id: str = typer.Argument(..., help="UUID of the key to revoke"),
) -> None:
    """Revoke a key so it can no longer authenticate."""
    from src.api.auth.keys import revoke_api_key

    try:
        parsed = uuid.UUID(key_id)
    except ValueError:
        console.print("[red]Error:[/red] key id must be a UUID")
        raise typer.Exit(1)

    try:
        info = revoke_api_key(parsed)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)

    if info is None:
        console.print(f"[red]Error:[/red] no API key with id {key_id}")
        raise typer.Exit(1)

    console.print(f"[green]Revoked[/green] {info.id} (prefix {info.key_prefix})")
