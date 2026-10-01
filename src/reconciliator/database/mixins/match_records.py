"""Match record streaming and entity keys repository mixin."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from typing import Optional

from sqlalchemy import and_, or_, select

from reconciliator.database.schema import (
    entity_keys_table,
    expressions_table,
    identifiers_table,
    manifestations_table,
    works_table,
)
from reconciliator.models import MatchRecord
from reconciliator.utils.curie import to_curie

logger = logging.getLogger(__name__)


class MatchRecordRepositoryMixin:
    """Repository mixin for entity keys and match record streaming."""

    def get_all_entity_keys(
        self, entity_type: str = "manifestation"
    ) -> dict[int, dict[str, list[str]]]:
        """Return key bags for every entity of a type, keyed by entity_id.

        Bulk form for the clusterer, which needs keys for the whole corpus to
        build its blocking index and would otherwise issue one query per record.
        """
        out: dict[int, dict[str, list[str]]] = {}
        stmt = (
            select(
                entity_keys_table.c.entity_id,
                entity_keys_table.c.key_type,
                entity_keys_table.c.key_value,
            )
            .where(entity_keys_table.c.entity_type == entity_type)
            .order_by(entity_keys_table.c.entity_id, entity_keys_table.c.id)
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        for row in rows:
            out.setdefault(row["entity_id"], {}).setdefault(row["key_type"], []).append(
                row["key_value"]
            )
        return out

    def get_entity_keys_for_ids(
        self, entity_ids: Sequence[int], entity_type: str = "manifestation"
    ) -> dict[int, dict[str, list[str]]]:
        """Return key bags for a specific sequence of entity IDs."""
        if not entity_ids:
            return {}
        out: dict[int, dict[str, list[str]]] = {}
        with self.get_connection() as conn:
            for i in range(0, len(entity_ids), 500):
                batch = list(entity_ids[i : i + 500])
                stmt = (
                    select(
                        entity_keys_table.c.entity_id,
                        entity_keys_table.c.key_type,
                        entity_keys_table.c.key_value,
                    )
                    .where(
                        entity_keys_table.c.entity_type == entity_type,
                        entity_keys_table.c.entity_id.in_(batch),
                    )
                    .order_by(entity_keys_table.c.entity_id, entity_keys_table.c.id)
                )
                rows = conn.execute(stmt).mappings().fetchall()
                for row in rows:
                    out.setdefault(row["entity_id"], {}).setdefault(row["key_type"], []).append(
                        row["key_value"]
                    )
        return out

    def get_match_records_stream(
        self,
        origins: Optional[list[str]] = None,
        chunk_size: int = 10000,
    ) -> Iterator[MatchRecord]:
        """Stream MatchRecords out of WEMI rows using keyset pagination."""
        last_id = -1
        while True:
            id_stmt = select(manifestations_table.c.id).where(manifestations_table.c.id > last_id)
            if origins:
                id_stmt = id_stmt.where(manifestations_table.c.record_origin.in_(origins))
            id_stmt = id_stmt.order_by(manifestations_table.c.id).limit(chunk_size)

            with self.get_connection() as conn:
                chunk_ids = list(conn.execute(id_stmt).scalars().all())

            if not chunk_ids:
                break

            last_id = chunk_ids[-1]

            stmt = (
                select(
                    manifestations_table.c.id.label("manif_id"),
                    manifestations_table.c.source.label("m_source"),
                    manifestations_table.c.source_id,
                    manifestations_table.c.title.label("m_title"),
                    manifestations_table.c.publication_year,
                    manifestations_table.c.volume_designation,
                    manifestations_table.c.is_component,
                    manifestations_table.c.serialized_text.label("m_serialized"),
                    manifestations_table.c.record_origin,
                    works_table.c.id.label("work_id"),
                    works_table.c.source.label("w_source"),
                    works_table.c.source_id.label("w_source_id"),
                    works_table.c.title.label("w_title"),
                    works_table.c.work_type,
                    works_table.c.serialized_text.label("w_serialized"),
                    expressions_table.c.source.label("e_source"),
                    expressions_table.c.source_id.label("e_source_id"),
                    expressions_table.c.language,
                    expressions_table.c.content_type,
                    expressions_table.c.serialized_text.label("e_serialized"),
                )
                .distinct(manifestations_table.c.id)
                .select_from(
                    manifestations_table.outerjoin(
                        works_table, works_table.c.id == manifestations_table.c.work_id
                    ).outerjoin(expressions_table, expressions_table.c.work_id == works_table.c.id)
                )
                .where(manifestations_table.c.id.in_(chunk_ids))
                .order_by(manifestations_table.c.id)
            )

            with self.get_connection() as conn:
                rows = conn.execute(stmt).mappings().fetchall()

                work_ids = [r["work_id"] for r in rows if r["work_id"] is not None]
                ident_by_entity: dict[tuple[str, int], dict[str, list[str]]] = {}
                for i in range(0, max(len(chunk_ids), len(work_ids) or 1), 500):
                    batch_manifs = chunk_ids[i : i + 500]
                    batch_works = work_ids[i : i + 500]
                    conds = []
                    if batch_manifs:
                        conds.append(
                            and_(
                                identifiers_table.c.entity_type == "manifestation",
                                identifiers_table.c.entity_id.in_(batch_manifs),
                            )
                        )
                    if batch_works:
                        conds.append(
                            and_(
                                identifiers_table.c.entity_type == "work",
                                identifiers_table.c.entity_id.in_(batch_works),
                            )
                        )
                    if conds:
                        ident_stmt = select(
                            identifiers_table.c.entity_type,
                            identifiers_table.c.entity_id,
                            identifiers_table.c.source.label("scheme"),
                            identifiers_table.c.source_id.label("value"),
                        ).where(or_(*conds))
                        ident_rows = conn.execute(ident_stmt).mappings().fetchall()
                        for r in ident_rows:
                            bag = ident_by_entity.setdefault((r["entity_type"], r["entity_id"]), {})
                            bucket = bag.setdefault(r["scheme"].lower(), [])
                            if r["value"] not in bucket:
                                bucket.append(r["value"])

            key_bags = self.get_entity_keys_for_ids(chunk_ids, "manifestation")

            seen_in_chunk: set[int] = set()
            for row in rows:
                if row["manif_id"] in seen_in_chunk:
                    continue
                seen_in_chunk.add(row["manif_id"])

                keys = key_bags.get(row["manif_id"], {})
                identifiers: dict[str, list[str]] = {}
                for et, eid in (
                    ("manifestation", row["manif_id"]),
                    ("work", row["work_id"]),
                    ("expression", None),
                ):
                    if eid is None:
                        continue
                    for scheme, values in ident_by_entity.get((et, eid), {}).items():
                        bucket = identifiers.setdefault(scheme, [])
                        for v in values:
                            if v not in bucket:
                                bucket.append(v)

                if row["source_id"]:
                    bucket = identifiers.setdefault("control_number", [])
                    if row["source_id"] not in bucket:
                        bucket.append(row["source_id"])

                title = row["m_title"] or row["w_title"] or ""
                yield MatchRecord(
                    record_id=row["source_id"],
                    format="wemi",
                    work_type=row["work_type"] or "text",
                    title=title,
                    uniform_title=(keys.get("uniform_title") or [None])[0],
                    language=(
                        None if (row["language"] or "").lower() == "und" else row["language"]
                    ),
                    author_key=(keys.get("author") or [None])[0],
                    title_stem=(keys.get("title_stem") or [None])[0] or title,
                    keys=keys,
                    identifiers=identifiers,
                    content_type=row["content_type"],
                    work_curie=(
                        to_curie(row["w_source"], row["w_source_id"]) if row["w_source"] else None
                    ),
                    expression_curie=(
                        to_curie(row["e_source"], row["e_source_id"]) if row["e_source"] else None
                    ),
                    manifestation_curie=(
                        to_curie(row["m_source"], row["source_id"]) if row["m_source"] else None
                    ),
                    year=row["publication_year"],
                    volume_designation=row["volume_designation"],
                    is_component=bool(row["is_component"]),
                    serialized_work=row["w_serialized"],
                    serialized_expression=row["e_serialized"],
                    serialized_manifestation=row["m_serialized"],
                )

    def get_all_match_records(self, origins: Optional[list[str]] = None) -> list[MatchRecord]:
        """Project every stored record back out of WEMI rows as MatchRecords."""
        return list(self.get_match_records_stream(origins=origins))

    def find_entities_by_key(
        self, key_type: str, key_value: str, entity_type: str = "manifestation"
    ) -> list[int]:
        """Return entity ids carrying a given key, for blocking lookups."""
        stmt = select(entity_keys_table.c.entity_id).where(
            entity_keys_table.c.entity_type == entity_type,
            entity_keys_table.c.key_type == key_type,
            entity_keys_table.c.key_value == key_value,
        )
        with self.get_connection() as conn:
            return list(conn.execute(stmt).scalars().all())
