"""Serve command for REST API and Web UI."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from reconciliator.commands.common import (
    _resolve_cli_config,
    _wemi_repo,
    console,
    logger,
)
from reconciliator.config import AppConfig, configure_logging, load_config


def serve_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host address to bind"),
    port: int = typer.Option(8000, "--port", "-p", help="Port number to bind"),
    no_auth: bool = typer.Option(
        False, "--no-auth", help="Disable Microsoft Entra ID auth for local development"
    ),
    role: Optional[str] = typer.Option(
        None,
        "--role",
        help="Default user role when running with --no-auth (Admin, Curator, Reader)",
    ),
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn hot reloading"),
) -> None:
    """Launch Reconciliator REST API and Web UI server."""
    import uvicorn

    from reconciliator.api import create_app

    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config) if resolved_config.exists() else AppConfig()
    if no_auth:
        cfg.auth.enabled = False
        if role:
            cfg.auth.dev_default_role = role
    configure_logging(cfg.logging)

    wemi_repo = _wemi_repo(cfg)
    embedder = None
    try:
        from reconciliator.embedding import create_vector_embedder

        embedder = create_vector_embedder(cfg)
    except Exception as e:
        logger.debug("Embedder not loaded for API serve: %s", e)

    api_app = create_app(config=cfg, repo=wemi_repo, embedder=embedder)
    console.print(
        f"[bold green]Starting Reconciliator Web UI & API on http://{host}:{port}[/bold green]"
    )
    if not cfg.auth.enabled:
        console.print(
            f"[yellow]Local Dev Mode enabled (auth bypassed, default role: {cfg.auth.dev_default_role})[/yellow]"
        )
    console.print(f"[cyan]API Documentation: http://{host}:{port}/api/docs[/cyan]")
    uvicorn.run(api_app, host=host, port=port)
