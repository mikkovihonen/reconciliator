"""WEMI authority commands: consolidation, clustering, and index building."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from reconciliator.commands.common import _resolve_cli_config, _wemi_repo, console
from reconciliator.config import configure_logging, load_config


def consolidate_works_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report the groups that would be formed without writing"
    ),
    reset: bool = typer.Option(
        False, "--reset", help="Discard existing consolidation before running"
    ),
) -> None:
    """Consolidate work rows that different sources hold for the same work.

    Each source mints its own work URI, so one work held by three catalogues is
    three rows. This elects one canonical row per equivalence group and records
    same_work_as edges explaining why. Nothing is deleted, and the run is
    repeatable after ingesting further sources.
    """
    from reconciliator.reconciliation.consolidation import WorkConsolidator

    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)

    # Typer's OptionInfo defaults are truthy, so guard for direct invocation.
    dry_run = dry_run if isinstance(dry_run, bool) else False
    reset = reset if isinstance(reset, bool) else False

    wemi_repo = _wemi_repo(cfg)
    if reset:
        wemi_repo.clear_canonical_works()
        console.print("[yellow]Cleared existing work consolidation.[/yellow]")

    consolidator = WorkConsolidator(repo=wemi_repo, config=cfg.consolidation)
    if not cfg.consolidation.enabled:
        console.print("[yellow]Work consolidation is disabled in configuration.[/yellow]")
        raise typer.Exit(code=0)

    signatures = consolidator.load_signatures()
    console.print(f"[cyan]Comparing {len(signatures)} authority works...[/cyan]")
    result = consolidator.consolidate(dry_run=dry_run)

    if not result.groups:
        console.print("[green]No cross-source duplicates found.[/green]")
        return

    table = Table(title="Work consolidation groups")
    table.add_column("Canonical work", overflow="fold")
    table.add_column("Folded in", overflow="fold")
    table.add_column("Evidence")
    for members in result.groups:
        canonical = result.canonical_by_work[members[0]]
        others = [m for m in members if m != canonical]
        evidence = sorted(
            {ev for (a, b), ev in result.evidence.items() if a in members and b in members}
        )
        table.add_row(
            signatures[canonical].work_uri,
            "\n".join(signatures[m].work_uri for m in others),
            ", ".join(evidence),
        )
    console.print(table)

    verb = "Would consolidate" if dry_run else "Consolidated"
    console.print(
        f"[green]{verb} {result.consolidated_works} work rows into "
        f"{len(result.groups)} canonical works"
        + ("" if dry_run else f" ({result.edges_written} same_work_as edges).")
        + "[/green]"
    )
    if dry_run:
        console.print("[yellow]Dry run: nothing was written.[/yellow]")


def cluster_agents_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    threshold: Optional[float] = typer.Option(
        None, "--threshold", help="Clustering similarity threshold override"
    ),
) -> None:
    """Cluster agent entities across sources into canonical agent clusters."""
    # Typer's OptionInfo defaults are truthy, so guard for direct invocation.
    threshold = threshold if isinstance(threshold, (int, float)) else None

    from reconciliator.clustering import AgentClusterer

    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)

    cluster_config = cfg.agent_clustering
    if threshold is not None:
        cluster_config.similarity_threshold = threshold

    wemi_repo = _wemi_repo(cfg)
    clusterer = AgentClusterer(repo=wemi_repo, config=cluster_config)

    result = clusterer.cluster_agents()
    console.print(
        f"[green]Clustered {result.total_agents} agents into {result.total_clusters} canonical clusters "
        f"({result.multi_member_clusters} multi-member, max size {result.max_cluster_size}).[/green]"
    )


def build_agent_index_cmd(
    config_path: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to config.yaml"
    ),
    batch_size: int = typer.Option(128, "--batch-size", "-b", help="Encoding batch size"),
) -> None:
    """Build dedicated agent vector index (agent.index) using concise essential serialization."""
    from reconciliator.indexer import create_vector_engine

    cfg = load_config(config_path)
    wemi_repo = _wemi_repo(cfg)
    from reconciliator.embedding import create_vector_embedder

    embedder = create_vector_embedder(cfg)
    agent_indexer = create_vector_engine(
        cfg.storage.agent_index,
        dimension=embedder.dimension,
        config=cfg.storage.vector_engine,
    )
    console.print(f"[cyan]Building agent index at {cfg.storage.agent_index}...[/cyan]")
    tpl_path = cfg.reconcile.templates.agent if cfg.reconcile else None
    count = wemi_repo.build_agent_index(
        agent_indexer=agent_indexer,
        embedder=embedder,
        batch_size=batch_size,
        template_path=tpl_path,
    )
    console.print(
        f"[green]Successfully indexed {count} agents into {cfg.storage.agent_index}[/green]"
    )


def build_index_cmd(
    config_path: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to config.yaml"
    ),
    batch_size: int = typer.Option(128, "--batch-size", "-b", help="Encoding batch size"),
    tier: str = typer.Option(
        "all", "--tier", "-t", help="Which index to build: work, manifestation, or all"
    ),
) -> None:
    """Build the work and manifestation vector indexes from stored serializations.

    Two tiers, each with one consumer: the work index serves the matcher's
    semantic fallback, the manifestation index serves the clusterer's
    near-duplicate detection. There is no expression tier.
    """
    from reconciliator.indexer import create_vector_engine

    cfg = load_config(config_path)
    from reconciliator.embedding import create_vector_embedder

    embedder = create_vector_embedder(cfg)
    wemi_repo = _wemi_repo(cfg, embedder=embedder)

    tier_val = tier.default if hasattr(tier, "default") else tier
    batch_size_val = batch_size.default if hasattr(batch_size, "default") else batch_size
    batch_size = int(batch_size_val)

    chosen = str(tier_val).lower()
    if chosen not in ("all", "work", "manifestation"):
        console.print(f"[red]Unknown tier '{tier_val}'. Use work, manifestation, or all.[/red]")
        raise typer.Exit(code=2)

    work_indexer = manifestation_indexer = None
    if chosen in ("all", "work"):
        work_indexer = create_vector_engine(
            cfg.storage.work_index, dimension=embedder.dimension, config=cfg.storage.vector_engine
        )
    if chosen in ("all", "manifestation"):
        manifestation_indexer = create_vector_engine(
            cfg.storage.manifestation_index,
            dimension=embedder.dimension,
            config=cfg.storage.vector_engine,
        )

    console.print("[cyan]Embedding stored works and manifestations...[/cyan]")
    counts = wemi_repo.build_wemi_indexes(
        embedder=embedder,
        work_indexer=work_indexer,
        manifestation_indexer=manifestation_indexer,
        batch_size=batch_size,
        include_constituent_works=cfg.constituents.index_works,
    )
    console.print(
        f"[green]Indexed {counts['works']} works into {cfg.storage.work_index} and "
        f"{counts['manifestations']} manifestations into {cfg.storage.manifestation_index}[/green]"
    )


def cluster_corpus_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    method: Optional[str] = typer.Option(
        None, "--method", "-m", help="Clustering method override: 'bigclust' or 'multiplex_graph'"
    ),
    max_work_clusters: Optional[int] = typer.Option(
        None, "--max-work-clusters", help="Max work clusters upper bound (k_max for BigClust)"
    ),
    max_expression_clusters: Optional[int] = typer.Option(
        None, "--max-expression-clusters", help="Max expression clusters per work family"
    ),
    source: Optional[str] = typer.Option(
        None, "--source", "-s", help="Filter clustering to a specific corpus source origin"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Evaluate clustering without persisting clusters to repository"
    ),
) -> None:
    from reconciliator.config.matching import BigClustConfig, WEMIClusterConfig

    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)

    method_val = method.default if hasattr(method, "default") else method
    max_w_val = (
        max_work_clusters.default
        if hasattr(max_work_clusters, "default")
        else max_work_clusters
    )
    max_e_val = (
        max_expression_clusters.default
        if hasattr(max_expression_clusters, "default")
        else max_expression_clusters
    )
    source_val = source.default if hasattr(source, "default") else source
    dry_run_val = dry_run.default if hasattr(dry_run, "default") else dry_run

    wemi_repo = _wemi_repo(cfg)
    cluster_cfg = (cfg.reconcile.cluster if cfg.reconcile else None) or WEMIClusterConfig()


    if method_val:
        cluster_cfg.method = str(method_val).strip()

    if max_w_val is not None or max_e_val is not None:
        if cluster_cfg.bigclust is None:
            cluster_cfg.bigclust = BigClustConfig()
        if max_w_val is not None:
            cluster_cfg.bigclust.max_work_clusters = int(max_w_val)
        if max_e_val is not None:
            cluster_cfg.bigclust.max_expression_clusters_per_work = int(max_e_val)

    embedder = None
    try:
        from reconciliator.embedding import create_vector_embedder

        embedder = create_vector_embedder(cfg)
    except Exception as e:
        console.print(
            f"[yellow]Notice: vector embedder could not be loaded ({e}), using default.[/yellow]"
        )

    origins = [str(source_val)] if source_val else (wemi_repo.non_authoritative_sources() or None)

    console.print(
        f"[cyan]Executing corpus clustering (method='{cluster_cfg.method}', origins={origins})...[/cyan]"
    )

    if cluster_cfg.method == "bigclust":
        from reconciliator.clustering.bigclust import BigClustMultiplexClusterer

        bc_cfg = cluster_cfg.bigclust
        max_w = bc_cfg.max_work_clusters if bc_cfg else 50
        max_e = bc_cfg.max_expression_clusters_per_work if bc_cfg else 10

        records = wemi_repo.get_all_match_records(origins=origins)
        if not records:
            console.print("[yellow]No staged records found to cluster.[/yellow]")
            return

        clusterer = BigClustMultiplexClusterer(
            corpus_repo=wemi_repo,
            embedder=embedder,
            max_work_clusters=max_w,
            max_expression_clusters_per_work=max_e,
        )
        res = clusterer.cluster_records(records, persist=not bool(dry_run_val))
    else:
        from reconciliator.clustering.corpus import CorpusClusterer

        clusterer = CorpusClusterer(
            corpus_repo=wemi_repo,
            wemi_repo=wemi_repo,
            config=cluster_cfg,
            corpus_origins=origins,
        )
        res = clusterer.cluster_corpus()

    verb = "Evaluated" if dry_run_val else "Persisted"
    console.print(
        f"[green]{verb} {res.records_clustered} staged record(s) into "
        f"{res.work_clusters_count} Work cluster(s), {res.expression_clusters_count} Expression cluster(s), "
        f"and {res.manifestation_clusters_count} Manifestation cluster(s) in {res.elapsed_seconds:.2f}s.[/green]"
    )

