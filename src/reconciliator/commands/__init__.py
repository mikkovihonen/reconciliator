"""CLI commands package and Typer application definition."""

from __future__ import annotations

import typer

from reconciliator.commands.daemon import daemon_app
from reconciliator.commands.db import db_app
from reconciliator.commands.ingest import (
    download_skosmos_cmd,
    ingest_oai_cmd,
    ingest_vocab_cmd,
    ingest_wikidata_cmd,
)
from reconciliator.commands.maintenance import (
    prune_cmd,
    reset_cmd,
    status_cmd,
)
from reconciliator.commands.pipeline import run_cmd
from reconciliator.commands.reconcile import reconcile_cmd
from reconciliator.commands.serve import serve_cmd
from reconciliator.commands.wemi import (
    build_agent_index_cmd,
    build_index_cmd,
    cluster_agents_cmd,
    cluster_corpus_cmd,
    consolidate_works_cmd,
)

app = typer.Typer(
    help="Reconciliator: High-Performance Finnish Bibliographic Authority Reconciliation Engine"
)

# Sub-command groups
app.add_typer(db_app, name="db")
app.add_typer(daemon_app, name="daemon")

# Root commands
app.command("reset")(reset_cmd)
app.command("status")(status_cmd)
app.command("prune")(prune_cmd)
app.command("download-skosmos")(download_skosmos_cmd)
app.command("ingest-vocab")(ingest_vocab_cmd)
app.command("ingest-oai")(ingest_oai_cmd)
app.command("ingest-wikidata")(ingest_wikidata_cmd)
app.command("reconcile")(reconcile_cmd)
app.command("consolidate-works")(consolidate_works_cmd)
app.command("cluster-agents")(cluster_agents_cmd)
app.command("cluster-corpus")(cluster_corpus_cmd)
app.command("build-agent-index")(build_agent_index_cmd)
app.command("build-index")(build_index_cmd)
app.command("run")(run_cmd)
app.command("serve")(serve_cmd)



def main() -> None:
    """Main CLI entrypoint."""
    app()


__all__ = [
    "app",
    "main",
    "db_app",
    "daemon_app",
    "reset_cmd",
    "status_cmd",
    "prune_cmd",
    "download_skosmos_cmd",
    "ingest_vocab_cmd",
    "ingest_oai_cmd",
    "ingest_wikidata_cmd",
    "reconcile_cmd",
    "consolidate_works_cmd",
    "cluster_agents_cmd",
    "build_agent_index_cmd",
    "build_index_cmd",
    "run_cmd",
    "serve_cmd",
]
