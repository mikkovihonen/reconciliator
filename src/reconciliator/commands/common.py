"""Shared CLI helpers, console instances, and configuration resolvers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

from reconciliator.database import WEMIRepository

console = Console()
logger = logging.getLogger("reconciliator.commands")


def _resolve_cli_config(config: Path | str) -> Path:
    """Resolve config path, falling back to config.example.yaml if config.yaml is absent."""
    p = Path(config)
    if not p.exists() and p == Path("config.yaml") and Path("config.example.yaml").exists():
        return Path("config.example.yaml")
    return p


def _wemi_repo(cfg: Any, embedder: Optional[Any] = None) -> WEMIRepository:
    """Build the repository the way every command should.

    Passing an `embedder` wires the work and manifestation vector indexes. Every
    CLI command used to construct the repository with `db_path` and `fts_config`
    only, which left `work_indexer` as None and so made the matcher's tier-2
    dense branch unreachable in production - every match fell through to FTS
    while the embedding configuration, engine selection and IPC daemon all sat
    there serving nothing.

    The embedder is what supplies the dimension; there is no configured value, so
    a command with no embedder gets no indexes rather than a guessed dimension
    that would not match the vectors already stored. Index files that do not
    exist yet are created empty, and both the matcher and the clusterer guard on
    `total_vectors == 0`, so wiring them before `build-index` has run is safe.
    """
    if embedder is None:
        return WEMIRepository(cfg.storage.database_url, fts_config=cfg.storage.fts)
    return WEMIRepository(
        cfg.storage.database_url,
        fts_config=cfg.storage.fts,
        work_index_path=cfg.storage.work_index,
        manifestation_index_path=cfg.storage.manifestation_index,
        dimension=embedder.dimension,
        vector_engine_config=cfg.storage.vector_engine,
    )
