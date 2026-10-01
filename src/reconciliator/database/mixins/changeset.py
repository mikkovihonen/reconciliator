"""Review changeset, audit transaction, and entity pinning repository mixin."""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import Table, and_, delete, func, insert, or_, select, update

from reconciliator.database.mixins.common import row_to_dict
from reconciliator.database.schema import (
    changeset_operations_table,
    corpus_agent_cluster_members_table,
    corpus_linkages_table,
    entity_pins_table,
    review_changesets_table,
    work_consolidations_table,
    work_relationships_table,
)
from reconciliator.models import ChangesetOperation, ReviewChangeset


class ChangesetRepositoryMixin:
    """Repository mixin for curation changesets, transaction rollback, and entity pinning."""

    def get_connection(self) -> Any:
        raise NotImplementedError

    def create_changeset(
        self,
        title: str,
        description: Optional[str],
        created_by: str,
        assigned_to: Optional[str] = None,
    ) -> ReviewChangeset:
        """Create a new staged review changeset."""
        stmt = (
            insert(review_changesets_table)
            .values(
                title=title,
                description=description,
                status="draft",
                created_by=created_by,
                assigned_to=assigned_to,
            )
            .returning(review_changesets_table)
        )
        with self.get_connection() as conn:
            row = conn.execute(stmt).mappings().fetchone()
            return ReviewChangeset.model_validate(row_to_dict(row))

    def list_changesets(
        self,
        status: Optional[str] = None,
        created_by: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ReviewChangeset], int]:
        """List review changesets with optional status or author filtering."""
        stmt = select(review_changesets_table)
        count_stmt = select(func.count()).select_from(review_changesets_table)

        if status:
            stmt = stmt.where(review_changesets_table.c.status == status)
            count_stmt = count_stmt.where(review_changesets_table.c.status == status)
        if created_by:
            stmt = stmt.where(review_changesets_table.c.created_by == created_by)
            count_stmt = count_stmt.where(review_changesets_table.c.created_by == created_by)

        stmt = (
            stmt.order_by(review_changesets_table.c.created_at.desc()).limit(limit).offset(offset)
        )

        with self.get_connection() as conn:
            total = conn.execute(count_stmt).scalar() or 0
            rows = conn.execute(stmt).mappings().fetchall()
            results: list[ReviewChangeset] = []
            for r in rows:
                cs = ReviewChangeset.model_validate(row_to_dict(r))
                op_count_stmt = (
                    select(func.count())
                    .select_from(changeset_operations_table)
                    .where(changeset_operations_table.c.changeset_id == cs.id)
                )
                cs.operations_count = int(conn.execute(op_count_stmt).scalar() or 0)
                results.append(cs)
            return results, int(total)

    def get_changeset(self, changeset_id: int) -> Optional[ReviewChangeset]:
        """Fetch changeset by ID along with its staged operations."""
        stmt = select(review_changesets_table).where(review_changesets_table.c.id == changeset_id)
        with self.get_connection() as conn:
            row = conn.execute(stmt).mappings().fetchone()
            if not row:
                return None
            cs = ReviewChangeset.model_validate(row_to_dict(row))

            ops_stmt = (
                select(changeset_operations_table)
                .where(changeset_operations_table.c.changeset_id == changeset_id)
                .order_by(changeset_operations_table.c.sequence.asc())
            )
            op_rows = conn.execute(ops_stmt).mappings().fetchall()
            operations: list[ChangesetOperation] = []
            for op_r in op_rows:
                d = row_to_dict(op_r)
                if isinstance(d.get("parameters_json"), str):
                    try:
                        d["parameters"] = json.loads(d["parameters_json"])
                    except Exception:
                        d["parameters"] = {}
                elif isinstance(d.get("parameters"), dict):
                    pass
                else:
                    d["parameters"] = {}

                if isinstance(d.get("pre_image_json"), str):
                    try:
                        d["pre_image"] = json.loads(d["pre_image_json"])
                    except Exception:
                        d["pre_image"] = None

                operations.append(ChangesetOperation.model_validate(d))

            cs.operations = operations
            cs.operations_count = len(operations)
            return cs

    def add_changeset_operation(
        self,
        changeset_id: int,
        operation_type: str,
        entity_type: str,
        entity_id: str,
        target_id: Optional[str] = None,
        parameters: Optional[dict[str, Any]] = None,
        pre_image: Optional[dict[str, Any]] = None,
        notes: Optional[str] = None,
    ) -> ChangesetOperation:
        """Stage an operation inside a review changeset."""
        params_json = json.dumps(parameters or {}, ensure_ascii=False)
        pre_img_json = json.dumps(pre_image, ensure_ascii=False) if pre_image is not None else None

        with self.get_connection() as conn:
            seq_stmt = select(
                func.coalesce(func.max(changeset_operations_table.c.sequence), 0)
            ).where(changeset_operations_table.c.changeset_id == changeset_id)
            next_seq = int(conn.execute(seq_stmt).scalar() or 0) + 1

            stmt = (
                insert(changeset_operations_table)
                .values(
                    changeset_id=changeset_id,
                    sequence=next_seq,
                    operation_type=operation_type,
                    entity_type=entity_type,
                    entity_id=str(entity_id),
                    target_id=str(target_id) if target_id is not None else None,
                    parameters_json=params_json,
                    pre_image_json=pre_img_json,
                    notes=notes,
                )
                .returning(changeset_operations_table)
            )
            row = conn.execute(stmt).mappings().fetchone()
            d = row_to_dict(row)
            d["parameters"] = parameters or {}
            d["pre_image"] = pre_image
            return ChangesetOperation.model_validate(d)

    def delete_changeset_operation(self, operation_id: int) -> bool:
        """Remove a staged operation from a changeset."""
        stmt = (
            delete(changeset_operations_table)
            .where(changeset_operations_table.c.id == operation_id)
            .returning(changeset_operations_table.c.id)
        )
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return row is not None

    def commit_changeset(self, changeset_id: int, committed_by: str) -> ReviewChangeset:
        """Atomically apply all staged operations in a changeset, pinning decisions."""
        cs = self.get_changeset(changeset_id)
        if not cs:
            raise ValueError(f"Changeset {changeset_id} not found")
        if cs.status == "committed":
            raise ValueError(f"Changeset {changeset_id} is already committed")

        def _safe_upsert(c: Any, tbl: Table, keys: list[str], vals: dict[str, Any]) -> None:
            cond = and_(*[tbl.c[k] == vals[k] for k in keys])
            existing_val = c.execute(select(tbl.c[keys[0]]).where(cond)).scalar()
            if existing_val is not None:
                updates = {k: v for k, v in vals.items() if k not in keys}
                if updates:
                    c.execute(update(tbl).where(cond).values(**updates))
            else:
                c.execute(insert(tbl).values(**vals))

        with self.get_connection() as conn:
            for op in cs.operations:
                op_type = op.operation_type
                params = op.parameters

                if op_type == "work_merge":
                    work_ids = [int(x) for x in params.get("work_ids", [op.entity_id])]
                    canonical_id = int(params.get("canonical_id", work_ids[0]))
                    for wid in work_ids:
                        _safe_upsert(
                            conn,
                            work_consolidations_table,
                            ["work_id"],
                            {
                                "work_id": wid,
                                "canonical_work_id": canonical_id,
                                "evidence": "manual_curation",
                            },
                        )
                        _safe_upsert(
                            conn,
                            entity_pins_table,
                            ["entity_type", "entity_id"],
                            {
                                "entity_type": "work_consolidation",
                                "entity_id": str(wid),
                                "changeset_id": changeset_id,
                                "pinned_by": committed_by,
                            },
                        )
                    for i in range(len(work_ids)):
                        for j in range(i + 1, len(work_ids)):
                            for src, tgt in (
                                (work_ids[i], work_ids[j]),
                                (work_ids[j], work_ids[i]),
                            ):
                                rel_cond = and_(
                                    work_relationships_table.c.source_work_id == src,
                                    work_relationships_table.c.target_work_id == tgt,
                                    work_relationships_table.c.relationship_type == "same_work_as",
                                )
                                if (
                                    conn.execute(
                                        select(work_relationships_table.c.id).where(rel_cond)
                                    ).scalar()
                                    is None
                                ):
                                    conn.execute(
                                        insert(work_relationships_table).values(
                                            source_work_id=src,
                                            target_work_id=tgt,
                                            relationship_type="same_work_as",
                                        )
                                    )

                elif op_type == "work_split":
                    wid = int(op.entity_id)
                    conn.execute(
                        delete(work_relationships_table).where(
                            and_(
                                work_relationships_table.c.relationship_type == "same_work_as",
                                or_(
                                    work_relationships_table.c.source_work_id == wid,
                                    work_relationships_table.c.target_work_id == wid,
                                ),
                            )
                        )
                    )
                    conn.execute(
                        delete(work_consolidations_table).where(
                            work_consolidations_table.c.work_id == wid
                        )
                    )
                    _safe_upsert(
                        conn,
                        entity_pins_table,
                        ["entity_type", "entity_id"],
                        {
                            "entity_type": "work_consolidation",
                            "entity_id": str(wid),
                            "changeset_id": changeset_id,
                            "pinned_by": committed_by,
                        },
                    )

                elif op_type == "work_elect_canonical":
                    canonical_id = int(op.target_id or op.entity_id)
                    member_ids = [int(x) for x in params.get("member_ids", [op.entity_id])]
                    for mid in member_ids:
                        conn.execute(
                            update(work_consolidations_table)
                            .where(work_consolidations_table.c.work_id == mid)
                            .values(canonical_work_id=canonical_id)
                        )
                        _safe_upsert(
                            conn,
                            entity_pins_table,
                            ["entity_type", "entity_id"],
                            {
                                "entity_type": "work_consolidation",
                                "entity_id": str(mid),
                                "changeset_id": changeset_id,
                                "pinned_by": committed_by,
                            },
                        )

                elif op_type == "agent_cluster_merge":
                    target_cluster_id = str(op.target_id or op.entity_id)
                    source_cluster_id = str(params.get("source_cluster_id", op.entity_id))
                    conn.execute(
                        update(corpus_agent_cluster_members_table)
                        .where(corpus_agent_cluster_members_table.c.cluster_id == source_cluster_id)
                        .values(cluster_id=target_cluster_id)
                    )
                    _safe_upsert(
                        conn,
                        entity_pins_table,
                        ["entity_type", "entity_id"],
                        {
                            "entity_type": "agent_cluster",
                            "entity_id": target_cluster_id,
                            "changeset_id": changeset_id,
                            "pinned_by": committed_by,
                        },
                    )

                elif op_type == "agent_cluster_split":
                    agent_id = int(op.entity_id)
                    conn.execute(
                        delete(corpus_agent_cluster_members_table).where(
                            corpus_agent_cluster_members_table.c.agent_id == agent_id
                        )
                    )
                    _safe_upsert(
                        conn,
                        entity_pins_table,
                        ["entity_type", "entity_id"],
                        {
                            "entity_type": "agent_cluster_member",
                            "entity_id": str(agent_id),
                            "changeset_id": changeset_id,
                            "pinned_by": committed_by,
                        },
                    )

                elif op_type == "reconciliation_relink":
                    record_id = str(op.entity_id)
                    new_work_uri = str(params.get("work_uri", ""))
                    new_exp_uri = str(params.get("expression_uri", ""))
                    conn.execute(
                        update(corpus_linkages_table)
                        .where(corpus_linkages_table.c.record_id == record_id)
                        .values(
                            final_work_source="manual",
                            final_work_source_id=new_work_uri,
                            final_expression_source="manual",
                            final_expression_source_id=new_exp_uri,
                        )
                    )
                    _safe_upsert(
                        conn,
                        entity_pins_table,
                        ["entity_type", "entity_id"],
                        {
                            "entity_type": "corpus_linkage",
                            "entity_id": record_id,
                            "changeset_id": changeset_id,
                            "pinned_by": committed_by,
                        },
                    )

                elif op_type == "reconciliation_reject":
                    record_id = str(op.entity_id)
                    conn.execute(
                        update(corpus_linkages_table)
                        .where(corpus_linkages_table.c.record_id == record_id)
                        .values(
                            final_work_source="",
                            final_work_source_id="",
                            final_expression_source="",
                            final_expression_source_id="",
                        )
                    )
                    _safe_upsert(
                        conn,
                        entity_pins_table,
                        ["entity_type", "entity_id"],
                        {
                            "entity_type": "corpus_linkage",
                            "entity_id": record_id,
                            "changeset_id": changeset_id,
                            "pinned_by": committed_by,
                        },
                    )

            conn.execute(
                update(review_changesets_table)
                .where(review_changesets_table.c.id == changeset_id)
                .values(
                    status="committed",
                    committed_by=committed_by,
                    committed_at=func.current_timestamp(),
                    updated_at=func.current_timestamp(),
                )
            )

        return self.get_changeset(changeset_id) or cs

    def revert_changeset(self, changeset_id: int, reverted_by: str) -> ReviewChangeset:
        """Rollback a committed changeset using pre-images and release entity pins."""
        cs = self.get_changeset(changeset_id)
        if not cs:
            raise ValueError(f"Changeset {changeset_id} not found")
        if cs.status != "committed":
            raise ValueError(f"Changeset {changeset_id} is not committed (status: {cs.status})")

        with self.get_connection() as conn:
            conn.execute(
                delete(entity_pins_table).where(entity_pins_table.c.changeset_id == changeset_id)
            )

            for op in reversed(cs.operations):
                pre = op.pre_image or {}
                if not pre:
                    continue
                if op.operation_type in ("work_merge", "work_split", "work_elect_canonical"):
                    orig_canon = pre.get("canonical_work_id")
                    if orig_canon is not None:
                        conn.execute(
                            update(work_consolidations_table)
                            .where(work_consolidations_table.c.work_id == int(op.entity_id))
                            .values(canonical_work_id=int(orig_canon))
                        )
                elif op.operation_type in ("reconciliation_relink", "reconciliation_reject"):
                    orig_w = pre.get("final_work_source_id", "")
                    orig_e = pre.get("final_expression_source_id", "")
                    conn.execute(
                        update(corpus_linkages_table)
                        .where(corpus_linkages_table.c.record_id == str(op.entity_id))
                        .values(
                            final_work_source_id=orig_w,
                            final_expression_source_id=orig_e,
                        )
                    )

            conn.execute(
                update(review_changesets_table)
                .where(review_changesets_table.c.id == changeset_id)
                .values(
                    status="reverted",
                    updated_at=func.current_timestamp(),
                )
            )

        return self.get_changeset(changeset_id) or cs

    def get_pinned_entities(self, entity_type: Optional[str] = None) -> set[str]:
        """Fetch all entity IDs currently pinned by curators."""
        stmt = select(entity_pins_table.c.entity_id)
        if entity_type:
            stmt = stmt.where(entity_pins_table.c.entity_type == entity_type)
        with self.get_connection() as conn:
            return {str(r[0]) for r in conn.execute(stmt)}

    def is_entity_pinned(self, entity_type: str, entity_id: str) -> bool:
        """Check if an entity is pinned against automated pipeline modification."""
        stmt = (
            select(entity_pins_table.c.id)
            .where(
                and_(
                    entity_pins_table.c.entity_type == entity_type,
                    entity_pins_table.c.entity_id == str(entity_id),
                )
            )
            .limit(1)
        )
        with self.get_connection() as conn:
            return conn.execute(stmt).scalar() is not None
