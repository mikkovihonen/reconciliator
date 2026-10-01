"""Work consolidation and intra-cluster co-reference repository mixin."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import delete, select

from reconciliator.database.schema import (
    corpus_linkages_table,
    expression_clusters_table,
    manifestation_clusters_table,
    work_clusters_table,
    works_table,
)
from reconciliator.models import (
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    WorkCluster,
)

logger = logging.getLogger(__name__)


class ConsolidationRepositoryMixin:
    """Mixin providing work consolidation, clustering, and linkage queries."""

    engine: Any

    def consolidate_works(
        self,
        target_work_id: int,
        duplicate_work_ids: list[int],
        reason: str = "cluster_consolidation",
    ) -> None:
        """Merge duplicate works into a single canonical target work."""
        if not duplicate_work_ids:
            return

        with self.engine.begin() as conn:
            from reconciliator.database.schema import (
                contributions_table,
                expressions_table,
                manifestations_table,
                work_relationships_table,
            )

            conn.execute(
                expressions_table.update()
                .where(expressions_table.c.work_id.in_(duplicate_work_ids))
                .values(work_id=target_work_id)
            )

            conn.execute(
                manifestations_table.update()
                .where(manifestations_table.c.work_id.in_(duplicate_work_ids))
                .values(work_id=target_work_id)
            )

            conn.execute(
                contributions_table.update()
                .where(
                    contributions_table.c.entity_type == "work",
                    contributions_table.c.entity_id.in_(duplicate_work_ids),
                )
                .values(entity_id=target_work_id)
            )

            conn.execute(
                work_relationships_table.update()
                .where(work_relationships_table.c.source_work_id.in_(duplicate_work_ids))
                .values(source_work_id=target_work_id)
            )
            conn.execute(
                work_relationships_table.update()
                .where(work_relationships_table.c.target_work_id.in_(duplicate_work_ids))
                .values(target_work_id=target_work_id)
            )

            conn.execute(
                delete(works_table).where(works_table.c.id.in_(duplicate_work_ids))
            )

    def insert_work_cluster(self, cluster: WorkCluster) -> None:
        """Insert or replace a WorkCluster record in work_clusters."""
        with self.engine.begin() as conn:
            conn.execute(
                work_clusters_table.insert().values(
                    cluster_id=cluster.cluster_id,
                    canonical_work_uri=cluster.canonical_work_uri,
                    member_count=cluster.member_count,
                    signature=cluster.signature,
                )
            )

    def get_all_work_clusters(self) -> list[WorkCluster]:
        """Fetch all stored work clusters."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(work_clusters_table)).fetchall()
            return [
                WorkCluster(
                    cluster_id=r.cluster_id,
                    canonical_work_uri=r.canonical_work_uri,
                    member_count=r.member_count,
                    signature=r.signature,
                )
                for r in rows
            ]

    def insert_expression_cluster(self, cluster: ExpressionCluster) -> None:
        """Insert or replace an ExpressionCluster record in expression_clusters."""
        with self.engine.begin() as conn:
            conn.execute(
                expression_clusters_table.insert().values(
                    cluster_id=cluster.cluster_id,
                    canonical_expression_uri=cluster.canonical_expression_uri,
                    content_type=cluster.content_type,
                    language=cluster.language,
                    version_designation=cluster.version_designation,
                    member_count=cluster.member_count,
                )
            )

    def get_all_expression_clusters(self) -> list[ExpressionCluster]:
        """Fetch all stored expression clusters."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(expression_clusters_table)).fetchall()
            return [
                ExpressionCluster(
                    cluster_id=r.cluster_id,
                    canonical_expression_uri=r.canonical_expression_uri,
                    content_type=r.content_type,
                    language=r.language,
                    version_designation=r.version_designation,
                    member_count=r.member_count,
                )
                for r in rows
            ]

    def insert_manifestation_cluster(self, cluster: ManifestationCluster) -> None:
        """Insert or replace a ManifestationCluster record."""
        with self.engine.begin() as conn:
            conn.execute(
                manifestation_clusters_table.insert().values(
                    cluster_id=cluster.cluster_id,
                    canonical_manifestation_uri=cluster.canonical_manifestation_uri,
                    carrier_type=cluster.carrier_type,
                    publication_year=cluster.publication_year,
                    publisher=cluster.publisher,
                    member_count=cluster.member_count,
                )
            )

    def get_all_manifestation_clusters(self) -> list[ManifestationCluster]:
        """Fetch all stored manifestation clusters."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(manifestation_clusters_table)).fetchall()
            return [
                ManifestationCluster(
                    cluster_id=r.cluster_id,
                    canonical_manifestation_uri=r.canonical_manifestation_uri,
                    carrier_type=r.carrier_type,
                    publication_year=r.publication_year,
                    publisher=r.publisher,
                    member_count=r.member_count,
                )
                for r in rows
            ]

    def insert_corpus_linkage(self, linkage: CorpusLinkage) -> None:
        """Insert or update a CorpusLinkage record in corpus_linkages."""
        with self.engine.begin() as conn:
            conn.execute(
                corpus_linkages_table.insert().values(
                    record_id=linkage.record_id,
                    work_cluster_id=linkage.work_cluster_id,
                    expression_cluster_id=linkage.expression_cluster_id,
                    manifestation_cluster_id=linkage.manifestation_cluster_id,
                    final_work_uri=linkage.final_work_uri,
                    final_expression_uri=linkage.final_expression_uri,
                    final_manifestation_uri=linkage.final_manifestation_uri,
                )
            )

    def get_all_corpus_linkages(self) -> list[CorpusLinkage]:
        """Fetch all stored corpus linkages."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(corpus_linkages_table)).fetchall()
            return [
                CorpusLinkage(
                    record_id=r.record_id,
                    work_cluster_id=r.work_cluster_id,
                    expression_cluster_id=r.expression_cluster_id,
                    manifestation_cluster_id=r.manifestation_cluster_id,
                    final_work_uri=r.final_work_uri,
                    final_expression_uri=r.final_expression_uri,
                    final_manifestation_uri=r.final_manifestation_uri,
                )
                for r in rows
            ]

    def get_sibling_manifestation_record_ids(self, record_id: str) -> list[str]:
        """Return record_ids of sibling manifestations in the same work cluster."""
        with self.engine.connect() as conn:
            current = conn.execute(
                select(corpus_linkages_table.c.work_cluster_id).where(
                    corpus_linkages_table.c.record_id == record_id
                )
            ).fetchone()
            if not current or not current[0]:
                return []

            w_cid = current[0]
            sibs = conn.execute(
                select(corpus_linkages_table.c.record_id).where(
                    corpus_linkages_table.c.work_cluster_id == w_cid,
                    corpus_linkages_table.c.record_id != record_id,
                )
            ).fetchall()
            return [r[0] for r in sibs]

