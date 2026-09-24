"""Database container management subcommands (Podman PostgreSQL)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from reconciliator.commands.common import _resolve_cli_config, console
from reconciliator.config import load_config
from reconciliator.database import (
    get_container_storage_mount,
    is_container_present,
    is_container_running,
    migrate_container_storage_to_host,
    start_postgres_container,
    stop_postgres_container,
)

db_app = typer.Typer(help="Manage local PostgreSQL container via Podman")


@db_app.command("start")
def db_start_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    data_dir: Optional[Path] = typer.Option(
        None, "--data-dir", "-d", help="Host directory path to bind-mount as PostgreSQL storage"
    ),
    recreate: bool = typer.Option(
        False, "--recreate", help="Recreate container if it already exists"
    ),
    timeout: float = typer.Option(
        30.0, "--timeout", "-t", help="Timeout in seconds to wait for PostgreSQL readiness"
    ),
) -> None:
    """Start local containerized PostgreSQL instance using Podman with optional host bind mount."""
    cfg_path = _resolve_cli_config(config)
    cfg = load_config(cfg_path)
    pg = cfg.storage.postgres
    target_data_dir = data_dir or pg.get_effective_data_dir()

    console.print(
        f"[bold blue]Starting PostgreSQL container '{pg.container_name}' on port {pg.port}...[/bold blue]"
    )
    if target_data_dir:
        console.print(
            f"[bold cyan]Using host bind mount for storage:[/bold cyan] {target_data_dir}"
        )

    try:
        start_postgres_container(
            host=pg.host,
            port=pg.port,
            database=pg.database,
            user=pg.user,
            password=pg.password,
            name=pg.container_name,
            image=pg.container_image,
            wait_ready=True,
            timeout_seconds=timeout,
            data_dir=target_data_dir,
            recreate=recreate,
        )
        console.print(
            f"[bold green]PostgreSQL is running and ready on {pg.host}:{pg.port}/{pg.database}[/bold green]"
        )
    except Exception as e:
        console.print(f"[bold red]Failed to start PostgreSQL container: {e}[/bold red]")
        raise typer.Exit(code=1)


@db_app.command("migrate-storage")
def db_migrate_storage_cmd(
    target_dir: Path = typer.Option(
        ..., "--target-dir", "-d", help="Host directory to copy PostgreSQL data to and bind mount"
    ),
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Safely migrate existing container data to a host directory bind mount without data loss."""
    cfg_path = _resolve_cli_config(config)
    cfg = load_config(cfg_path)
    pg = cfg.storage.postgres
    cname = pg.container_name

    console.print(
        f"[bold blue]Migrating container '{cname}' data to host directory: {target_dir}...[/bold blue]"
    )
    try:
        migrate_container_storage_to_host(target_host_dir=target_dir, name=cname)
        console.print(
            f"[bold green]Successfully migrated storage for '{cname}' to host directory {target_dir}. Container restarted with bind mount![/bold green]"
        )
    except Exception as e:
        console.print(f"[bold red]Storage migration failed: {e}[/bold red]")
        raise typer.Exit(code=1)


@db_app.command("stop")
def db_stop_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Stop the local PostgreSQL container."""
    cfg_path = _resolve_cli_config(config)
    cfg = load_config(cfg_path)
    cname = cfg.storage.postgres.container_name
    console.print(f"[bold yellow]Stopping PostgreSQL container '{cname}'...[/bold yellow]")
    if stop_postgres_container(name=cname):
        console.print(f"[bold green]Container '{cname}' stopped.[/bold green]")
    else:
        console.print(f"[bold yellow]Container '{cname}' was not running.[/bold yellow]")


@db_app.command("status")
def db_status_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Check status of the local PostgreSQL container."""
    cfg_path = _resolve_cli_config(config)
    cfg = load_config(cfg_path)
    pg = cfg.storage.postgres
    cname = pg.container_name
    running = is_container_running(name=cname)
    present = is_container_present(name=cname)
    eff_data_dir = pg.get_effective_data_dir()

    table = Table(title="PostgreSQL Container & Connection Status")
    table.add_column("Property", style="cyan")
    table.add_column("Value", style="green" if running else "yellow")

    table.add_row("Container Name", cname)
    table.add_row("Container Present", "Yes" if present else "No")
    table.add_row(
        "Container Running",
        "[bold green]Running[/bold green]" if running else "[bold red]Stopped[/bold red]",
    )
    table.add_row("Host / Port", f"{pg.host}:{pg.port}")
    table.add_row("Database", pg.database)
    table.add_row("User", pg.user)
    active_mount = get_container_storage_mount(cname) or (
        str(eff_data_dir) if eff_data_dir else None
    )
    table.add_row(
        "Storage Bind Mount",
        f"[bold cyan]{active_mount}[/bold cyan]"
        if active_mount
        else "None (Podman internal volume)",
    )

    console.print(table)

