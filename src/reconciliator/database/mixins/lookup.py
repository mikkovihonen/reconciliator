"""Entity lookup and retrieval repository mixin for WEMIRepository."""

from __future__ import annotations

import logging
import re
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from typing import Any, Optional

from sqlalchemy import and_, func, nulls_last, or_, select

from reconciliator.database.mixins.common import row_to_dict
from reconciliator.database.schema import (
    agent_attributes_table,
    agents_table,
    contributions_table,
    entity_keys_table,
    entity_subjects_table,
    expressions_table,
    identifiers_table,
    items_table,
    manifestation_expressions_table,
    manifestations_table,
    work_relationships_table,
    works_table,
)
from reconciliator.models import (
    AgentEntity,
    ContributionEntity,
    EntitySubject,
    EntityType,
    ExpressionEntity,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    RecordOrigin,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.utils.curie import parse_curie

logger = logging.getLogger(__name__)


def _row_to_agent(row: Any) -> AgentEntity:
    return AgentEntity.model_validate(row_to_dict(row))


def _row_to_work(row: Any) -> WorkEntity:
    return WorkEntity.model_validate(row_to_dict(row))


def _row_to_expression(row: Any) -> ExpressionEntity:
    return ExpressionEntity.model_validate(row_to_dict(row))


def _row_to_manifestation(row: Any) -> ManifestationEntity:
    return ManifestationEntity.model_validate(row_to_dict(row))


def _row_to_item(row: Any) -> ItemEntity:
    return ItemEntity.model_validate(row_to_dict(row))


def _row_to_identifier(row: Any) -> IdentifierEntity:
    return IdentifierEntity.model_validate(row_to_dict(row))


def _row_to_contribution(row: Any) -> ContributionEntity:
    return ContributionEntity.model_validate(row_to_dict(row))


def _row_to_work_relationship(row: Any) -> WorkRelationship:
    return WorkRelationship.model_validate(row_to_dict(row))


class EntityLookupRepositoryMixin:
    """Repository mixin providing lookups, relational traversals, and LRU caches for entities."""

    entity_cache_size: int
    _work_cache: OrderedDict[int, WorkEntity]
    _manifestation_cache: OrderedDict[int, ManifestationEntity]
    _expressions_for_work_cache: OrderedDict[int, list[ExpressionEntity]]
    _identifier_lookup_cache: OrderedDict[tuple[str, str, bool], list[tuple[EntityType, int]]]

    def get_connection(self) -> Any:
        raise NotImplementedError

    def get_read_connection(self) -> Any:
        raise NotImplementedError

    def authoritative_sources(self) -> list[str]:
        raise NotImplementedError
    def get_entity_keys(self, entity_type: str, entity_id: int) -> dict[str, list[str]]:
        """Return the key bag for one entity, shaped as the funnels consume it."""
        bag: dict[str, list[str]] = {}
        stmt = (
            select(entity_keys_table.c.key_type, entity_keys_table.c.key_value)
            .where(
                entity_keys_table.c.entity_type == entity_type,
                entity_keys_table.c.entity_id == entity_id,
            )
            .order_by(entity_keys_table.c.id)
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        for row in rows:
            bag.setdefault(row["key_type"], []).append(row["key_value"])
        return bag

    def get_subjects_for_entity(self, entity_type: str, entity_id: int) -> list[EntitySubject]:
        """Fetch all EntitySubject records linked to a specific entity."""
        stmt = (
            select(entity_subjects_table)
            .where(
                entity_subjects_table.c.entity_type == entity_type,
                entity_subjects_table.c.entity_id == entity_id,
            )
            .order_by(entity_subjects_table.c.is_primary.desc(), entity_subjects_table.c.id.asc())
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [EntitySubject.model_validate(dict(r)) for r in rows]

    def get_subjects_for_agent(self, agent_id: int) -> list[EntitySubject]:
        """Fetch all EntitySubject records linked to an agent (e.g. occupation, field_of_activity)."""
        return self.get_subjects_for_entity("agent", agent_id)

    def get_entities_by_concept(
        self,
        vocabulary: str,
        concept_id: str,
        entity_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Fetch entity subject rows linked to a specific vocabulary concept."""
        stmt = select(entity_subjects_table).where(
            entity_subjects_table.c.vocabulary == vocabulary,
            entity_subjects_table.c.concept_id == concept_id,
        )
        if entity_type:
            stmt = stmt.where(entity_subjects_table.c.entity_type == entity_type)
        stmt = stmt.order_by(entity_subjects_table.c.id.asc())
        with self.get_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [dict(r) for r in rows]

    def _get_entity_by_id(
        self, table: Any, row_converter: Callable[[Any], Any], entity_id: int
    ) -> Any:
        stmt = select(table).where(table.c.id == entity_id)
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return row_converter(row) if row else None

    def _get_entity_by_source(
        self,
        table: Any,
        row_converter: Callable[[Any], Any],
        source: str,
        source_id: str,
        case_insensitive: bool = False,
    ) -> Any:
        source_cond = (
            func.lower(table.c.source) == source.lower()
            if case_insensitive
            else table.c.source == source
        )
        stmt = select(table).where(source_cond, table.c.source_id == source_id)
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return row_converter(row) if row else None

    def get_agent(self, agent_id: int) -> Optional[AgentEntity]:
        """Fetch AgentEntity by ID with relational attributes and identifiers."""
        agents = self.get_agents_by_ids([agent_id])
        if agent_id in agents:
            return AgentEntity.model_validate(agents[agent_id])
        return None

    def get_agents_by_ids(self, agent_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Fetch agent profiles with relational attributes for a list of IDs."""
        if not agent_ids:
            return {}
        result: dict[int, dict[str, Any]] = {}
        with self.get_connection() as conn:
            # 1. Fetch agents
            agents_stmt = select(agents_table).where(agents_table.c.id.in_(agent_ids))
            agent_rows = conn.execute(agents_stmt).mappings().fetchall()
            for row in agent_rows:
                agent_id = row["id"]
                agent_dict = dict(row)
                agent_dict["qualifiers"] = []
                agent_dict["variant_names"] = []
                agent_dict["identifiers"] = []
                result[agent_id] = agent_dict

            # 2. Batch fetch attributes in a single query
            attr_stmt = select(
                agent_attributes_table.c.agent_id,
                agent_attributes_table.c.attribute_type,
                agent_attributes_table.c.value,
            ).where(agent_attributes_table.c.agent_id.in_(agent_ids))
            for r in conn.execute(attr_stmt):
                aid = r[0]
                atype = r[1]
                val = r[2]
                if aid in result:
                    if atype == "qualifier":
                        result[aid]["qualifiers"].append(val)
                    elif atype == "variant_name":
                        result[aid]["variant_names"].append(val)

            # 3. Batch fetch identifiers in a single query
            ident_stmt = select(identifiers_table).where(
                identifiers_table.c.entity_type == "agent",
                identifiers_table.c.entity_id.in_(agent_ids),
            )
            for r in conn.execute(ident_stmt):
                aid = r.entity_id if hasattr(r, "entity_id") else r[2]
                if aid in result:
                    result[aid]["identifiers"].append(_row_to_identifier(r))

        return result

    def get_agent_by_curie(self, curie_or_uri: str) -> Optional[AgentEntity]:
        """Fetch AgentEntity by CURIE or URI with relational attributes and identifiers."""
        source, source_id = parse_curie(curie_or_uri)
        if not source_id:
            return None
        with self.get_connection() as conn:
            stmt = select(agents_table.c.id).where(
                agents_table.c.source == source,
                agents_table.c.source_id == source_id,
            )
            row = conn.execute(stmt).fetchone()
            if not row:
                from reconciliator.config import get_source_aliases

                aliases = get_source_aliases(source)
                if aliases:
                    fallback_stmt = select(agents_table.c.id).where(
                        agents_table.c.source.in_([source, *aliases]),
                        agents_table.c.source_id == source_id,
                    )
                    row = conn.execute(fallback_stmt).fetchone()
        if row:
            return self.get_agent(row[0])
        return None

    def _enrich_work_identifiers(
        self, conn: Any, work: Optional[WorkEntity]
    ) -> Optional[WorkEntity]:
        if work and not work.wikidata_qid and work.id:
            stmt = (
                select(identifiers_table.c.source_id)
                .where(
                    identifiers_table.c.entity_type == "work",
                    identifiers_table.c.entity_id == work.id,
                    func.lower(identifiers_table.c.source) == "wikidata",
                )
                .limit(1)
            )
            qid = conn.execute(stmt).scalar()
            if qid:
                work.wikidata_qid = qid
        return work

    def get_work(self, work_id: int) -> Optional[WorkEntity]:
        """Fetch WorkEntity by ID with in-memory LRU caching."""
        if work_id in self._work_cache:
            self._work_cache.move_to_end(work_id)
            return self._work_cache[work_id]

        stmt = select(works_table).where(works_table.c.id == work_id)
        with self.get_read_connection() as conn:
            row = conn.execute(stmt).fetchone()
            work = self._enrich_work_identifiers(conn, _row_to_work(row)) if row else None

        if work is not None:
            self._work_cache[work_id] = work
            if len(self._work_cache) > self.entity_cache_size:
                self._work_cache.popitem(last=False)
        return work

    def get_works_by_ids(self, work_ids: list[int]) -> dict[int, WorkEntity]:
        """Fetch multiple WorkEntity instances by IDs, leveraging LRU cache."""
        if not work_ids:
            return {}
        result: dict[int, WorkEntity] = {}
        missing_ids: list[int] = []
        for wid in work_ids:
            if wid in self._work_cache:
                self._work_cache.move_to_end(wid)
                result[wid] = self._work_cache[wid]
            else:
                missing_ids.append(wid)

        if missing_ids:
            stmt = select(works_table).where(works_table.c.id.in_(missing_ids))
            with self.get_read_connection() as conn:
                for row in conn.execute(stmt):
                    w = self._enrich_work_identifiers(conn, _row_to_work(row))
                    if w and w.id:
                        result[w.id] = w
                        self._work_cache[w.id] = w
                        if len(self._work_cache) > self.entity_cache_size:
                            self._work_cache.popitem(last=False)
        return result

    def get_work_by_curie(self, curie_or_uri: str) -> Optional[WorkEntity]:
        """Fetch WorkEntity by CURIE or URI."""
        source, source_id = parse_curie(curie_or_uri)
        if not source_id:
            return None
        return self.get_work_by_source(source, source_id)

    def get_work_by_source(self, source: str, source_id: str) -> Optional[WorkEntity]:
        """Fetch WorkEntity by source and source_id."""
        stmt = select(works_table).where(
            works_table.c.source == source,
            works_table.c.source_id == source_id,
        )
        with self.get_read_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return self._enrich_work_identifiers(conn, _row_to_work(row)) if row else None

    def get_expression(self, expression_id: int) -> Optional[ExpressionEntity]:
        """Fetch ExpressionEntity by ID."""
        return self._get_entity_by_id(expressions_table, _row_to_expression, expression_id)

    def get_expression_by_curie(self, curie_or_uri: str) -> Optional[ExpressionEntity]:
        """Fetch ExpressionEntity by CURIE or URI."""
        source, source_id = parse_curie(curie_or_uri)
        if not source_id:
            return None
        return self._get_entity_by_source(
            expressions_table, _row_to_expression, source, source_id, case_insensitive=True
        )

    def get_manifestation(self, manifestation_id: int) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by ID with in-memory LRU caching."""
        if manifestation_id in self._manifestation_cache:
            self._manifestation_cache.move_to_end(manifestation_id)
            return self._manifestation_cache[manifestation_id]

        manif = self._get_entity_by_id(
            manifestations_table, _row_to_manifestation, manifestation_id
        )
        if manif is not None:
            self._manifestation_cache[manifestation_id] = manif
            if len(self._manifestation_cache) > self.entity_cache_size:
                self._manifestation_cache.popitem(last=False)
        return manif

    def get_manifestations_by_ids(
        self, manifestation_ids: list[int]
    ) -> dict[int, ManifestationEntity]:
        """Fetch multiple ManifestationEntity instances by IDs, leveraging LRU cache."""
        if not manifestation_ids:
            return {}
        result: dict[int, ManifestationEntity] = {}
        missing_ids: list[int] = []
        for mid in manifestation_ids:
            if mid in self._manifestation_cache:
                self._manifestation_cache.move_to_end(mid)
                result[mid] = self._manifestation_cache[mid]
            else:
                missing_ids.append(mid)

        if missing_ids:
            stmt = select(manifestations_table).where(manifestations_table.c.id.in_(missing_ids))
            with self.get_read_connection() as conn:
                for row in conn.execute(stmt):
                    m = _row_to_manifestation(row)
                    if m and m.id:
                        result[m.id] = m
                        self._manifestation_cache[m.id] = m
                        if len(self._manifestation_cache) > self.entity_cache_size:
                            self._manifestation_cache.popitem(last=False)
        return result

    def get_manifestation_by_source(
        self, source: str, source_id: str
    ) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by source and source_id."""
        return self._get_entity_by_source(
            manifestations_table, _row_to_manifestation, source, source_id
        )

    def get_expressions_for_work(
        self, work_id: int, record_origin: Optional[RecordOrigin | str] = None
    ) -> list[ExpressionEntity]:
        """Fetch all Expression entities linked to a given work_id, with optional record_origin filter."""
        if record_origin is None and work_id in self._expressions_for_work_cache:
            self._expressions_for_work_cache.move_to_end(work_id)
            return self._expressions_for_work_cache[work_id]

        stmt = select(expressions_table).where(expressions_table.c.work_id == work_id)
        if record_origin is not None:
            stmt = stmt.where(expressions_table.c.record_origin == record_origin)
        stmt = stmt.order_by(expressions_table.c.id)
        with self.get_read_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            exps = [_row_to_expression(r) for r in rows]

        if record_origin is None:
            self._expressions_for_work_cache[work_id] = exps
            if len(self._expressions_for_work_cache) > self.entity_cache_size:
                self._expressions_for_work_cache.popitem(last=False)
        return exps

    def get_constituent_works(self, work_id: int) -> list[WorkEntity]:
        """The works this one contains, by stored whole-part edges."""
        stmt = (
            select(
                works_table,
                func.min(work_relationships_table.c.sequence_number).label("seq"),
                func.min(manifestation_expressions_table.c.disc_number).label("d"),
                func.min(manifestation_expressions_table.c.track_number).label("t"),
            )
            .select_from(
                work_relationships_table.join(
                    works_table, works_table.c.id == work_relationships_table.c.target_work_id
                )
                .outerjoin(expressions_table, expressions_table.c.work_id == works_table.c.id)
                .outerjoin(
                    manifestation_expressions_table,
                    manifestation_expressions_table.c.expression_id == expressions_table.c.id,
                )
            )
            .where(
                work_relationships_table.c.source_work_id == work_id,
                work_relationships_table.c.relationship_type.in_(("has_part", "has_constituent")),
            )
            .group_by(*works_table.c)
            .order_by(
                nulls_last(func.min(work_relationships_table.c.sequence_number)),
                nulls_last(func.min(manifestation_expressions_table.c.disc_number)),
                nulls_last(func.min(manifestation_expressions_table.c.track_number)),
                works_table.c.id,
            )
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_work(r) for r in rows]

    def get_manifestation_by_curie(self, curie_or_uri: str) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by CURIE or URI."""
        source, source_id = parse_curie(curie_or_uri)
        if not source_id:
            return None
        return self.get_manifestation_by_source(source, source_id)

    def get_manifestations_for_work(
        self, work_id: int, record_origin: Optional[RecordOrigin | str] = None
    ) -> list[ManifestationEntity]:
        """Fetch all Manifestation entities linked directly to a given work_id, with optional record_origin filter."""
        stmt = select(manifestations_table).where(manifestations_table.c.work_id == work_id)
        if record_origin is not None:
            stmt = stmt.where(manifestations_table.c.record_origin == record_origin)
        stmt = stmt.order_by(
            manifestations_table.c.publication_year.asc(), manifestations_table.c.id.asc()
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_manifestation(r) for r in rows]

    def get_manifestations_for_works(
        self, work_ids: list[int], record_origin: Optional[RecordOrigin | str] = None
    ) -> dict[int, list[ManifestationEntity]]:
        """Fetch all Manifestation entities linked to given work_ids, grouped by work_id."""
        if not work_ids:
            return {}
        result: dict[int, list[ManifestationEntity]] = {wid: [] for wid in work_ids}
        chunk_size = 1000
        with self.get_connection() as conn:
            for i in range(0, len(work_ids), chunk_size):
                chunk = work_ids[i : i + chunk_size]
                stmt = select(manifestations_table).where(manifestations_table.c.work_id.in_(chunk))
                if record_origin is not None:
                    stmt = stmt.where(manifestations_table.c.record_origin == record_origin)
                stmt = stmt.order_by(
                    manifestations_table.c.publication_year.asc(), manifestations_table.c.id.asc()
                )
                rows = conn.execute(stmt).fetchall()
                for r in rows:
                    m = _row_to_manifestation(r)
                    if m.work_id in result:
                        result[m.work_id].append(m)
        return result

    def get_expressions_for_manifestation(
        self, manifestation_id: int, record_origin: Optional[RecordOrigin | str] = None
    ) -> list[ExpressionEntity]:
        """Fetch Expression entities associated with a manifestation via manifestation_expressions."""
        stmt = (
            select(expressions_table)
            .select_from(
                manifestation_expressions_table.join(
                    expressions_table,
                    manifestation_expressions_table.c.expression_id == expressions_table.c.id,
                )
            )
            .where(manifestation_expressions_table.c.manifestation_id == manifestation_id)
        )
        if record_origin is not None:
            stmt = stmt.where(expressions_table.c.record_origin == record_origin)
        stmt = stmt.order_by(
            manifestation_expressions_table.c.disc_number.asc(),
            manifestation_expressions_table.c.track_number.asc(),
            expressions_table.c.id.asc(),
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_expression(r) for r in rows]

    def get_manifestations_for_expression(
        self, expression_id: int, record_origin: Optional[RecordOrigin | str] = None
    ) -> list[ManifestationEntity]:
        """Fetch Manifestation entities associated with an expression via manifestation_expressions."""
        stmt = (
            select(manifestations_table)
            .select_from(
                manifestation_expressions_table.join(
                    manifestations_table,
                    manifestation_expressions_table.c.manifestation_id == manifestations_table.c.id,
                )
            )
            .where(manifestation_expressions_table.c.expression_id == expression_id)
        )
        if record_origin is not None:
            stmt = stmt.where(manifestations_table.c.record_origin == record_origin)
        stmt = stmt.order_by(
            manifestations_table.c.publication_year.asc(), manifestations_table.c.id.asc()
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_manifestation(r) for r in rows]

    def get_items_for_manifestation(
        self, manifestation_id: int, record_origin: Optional[RecordOrigin | str] = None
    ) -> list[ItemEntity]:
        """Fetch all Item entities held under a given manifestation."""
        stmt = select(items_table).where(items_table.c.manifestation_id == manifestation_id)
        if record_origin is not None:
            stmt = stmt.where(items_table.c.record_origin == record_origin)
        stmt = stmt.order_by(items_table.c.id)
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_item(r) for r in rows]

    def get_work_relationships(self, work_id: int) -> list[WorkRelationship]:
        """Fetch all relationships where work_id is either source or target."""
        stmt = (
            select(work_relationships_table)
            .where(
                or_(
                    work_relationships_table.c.source_work_id == work_id,
                    work_relationships_table.c.target_work_id == work_id,
                )
            )
            .order_by(work_relationships_table.c.id)
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_work_relationship(r) for r in rows]


    def get_identifiers_for_entity(
        self, entity_type: EntityType, entity_id: int
    ) -> list[IdentifierEntity]:
        """Fetch all polymorphic identifiers for a specific entity."""
        stmt = (
            select(identifiers_table)
            .where(
                identifiers_table.c.entity_type == entity_type,
                identifiers_table.c.entity_id == entity_id,
            )
            .order_by(identifiers_table.c.id)
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_identifier(r) for r in rows]

    def get_identifiers_for_entities(
        self, entity_type: EntityType | str, entity_ids: list[int]
    ) -> dict[int, list[IdentifierEntity]]:
        """Fetch all polymorphic identifiers for multiple entities of a given type in batch."""
        if not entity_ids:
            return {}
        result: dict[int, list[IdentifierEntity]] = {eid: [] for eid in entity_ids}
        chunk_size = 1000
        with self.get_connection() as conn:
            for i in range(0, len(entity_ids), chunk_size):
                chunk = entity_ids[i : i + chunk_size]
                stmt = (
                    select(identifiers_table)
                    .where(
                        identifiers_table.c.entity_type == entity_type,
                        identifiers_table.c.entity_id.in_(chunk),
                    )
                    .order_by(identifiers_table.c.id)
                )
                rows = conn.execute(stmt).fetchall()
                for r in rows:
                    ident = _row_to_identifier(r)
                    if ident.entity_id in result:
                        result[ident.entity_id].append(ident)
        return result

    def get_contributions_for_entity(
        self, entity_type: str, entity_id: int
    ) -> list[ContributionEntity]:
        """Fetch all contributions for a specific entity."""
        stmt = (
            select(contributions_table)
            .where(
                contributions_table.c.entity_type == entity_type,
                contributions_table.c.entity_id == entity_id,
            )
            .order_by(contributions_table.c.is_primary.desc(), contributions_table.c.id.asc())
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_contribution(r) for r in rows]

    def get_contributions_for_entities(
        self, entity_specs: list[tuple[str, int]]
    ) -> dict[tuple[str, int], list[ContributionEntity]]:
        """Fetch all contributions for multiple (entity_type, entity_id) pairs in batch."""
        if not entity_specs:
            return {}
        result: dict[tuple[str, int], list[ContributionEntity]] = {
            spec: [] for spec in entity_specs
        }
        by_type: dict[str, list[int]] = {}
        for etype, eid in entity_specs:
            by_type.setdefault(etype, []).append(eid)

        with self.get_connection() as conn:
            for etype, eids in by_type.items():
                chunk_size = 1000
                for i in range(0, len(eids), chunk_size):
                    chunk = eids[i : i + chunk_size]
                    stmt = (
                        select(contributions_table)
                        .where(
                            contributions_table.c.entity_type == etype,
                            contributions_table.c.entity_id.in_(chunk),
                        )
                        .order_by(
                            contributions_table.c.is_primary.desc(),
                            contributions_table.c.id.asc(),
                        )
                    )
                    rows = conn.execute(stmt).fetchall()
                    for r in rows:
                        c = _row_to_contribution(r)
                        key = (c.entity_type, c.entity_id)
                        if key in result:
                            result[key].append(c)
        return result

    def lookup_by_identifier(
        self, scheme: str, value: str, authoritative_only: bool = False
    ) -> list[tuple[EntityType, int]]:
        """Look up entity types and IDs carrying an identifier, optionally authoritative only."""
        cache_key = (scheme.lower().strip(), value.strip().lower(), authoritative_only)
        if cache_key in self._identifier_lookup_cache:
            self._identifier_lookup_cache.move_to_end(cache_key)
            return self._identifier_lookup_cache[cache_key]

        res = self.lookup_by_identifiers_batch(
            [(scheme, value)], authoritative_only=authoritative_only
        )
        ans = res.get((scheme, value), [])
        return ans

    def lookup_by_identifiers_batch(
        self, pairs: list[tuple[str, str]], authoritative_only: bool = False
    ) -> dict[tuple[str, str], list[tuple[EntityType, int]]]:
        """Look up entity types and IDs for multiple (scheme, value) pairs in bulk."""
        if not pairs:
            return {}

        results: dict[tuple[str, str], list[tuple[EntityType, int]]] = {}
        needed_pairs: list[tuple[str, str]] = []

        for pair in pairs:
            scheme, val = pair
            cache_key = (scheme.lower().strip(), val.strip().lower(), authoritative_only)
            if cache_key in self._identifier_lookup_cache:
                self._identifier_lookup_cache.move_to_end(cache_key)
                results[pair] = self._identifier_lookup_cache[cache_key]
            else:
                needed_pairs.append(pair)
                results[pair] = []

        if not needed_pairs:
            return results

        conds = []
        source_map: dict[str, list[tuple[str, str, str, tuple[str, str]]]] = defaultdict(list)
        for scheme, val in needed_pairs:
            src = scheme.lower().strip()
            if src == "wikidata_qid":
                src = "wikidata"
            val_clean = val.strip()
            norm_val = val_clean.lower()
            alnum_val = re.sub(r"[^0-9a-zA-Z]", "", val_clean).lower()
            source_map[src].append((val_clean, norm_val, alnum_val, (scheme, val)))

        for src, item_list in source_map.items():
            norm_vals = {norm_val for _, norm_val, _, _ in item_list}
            alnum_vals = {alnum_val for _, _, alnum_val, _ in item_list if alnum_val}
            clean_vals = {clean_val for clean_val, _, _, _ in item_list}
            combined_norms = list(norm_vals | alnum_vals)
            conds.append(
                and_(
                    identifiers_table.c.source == src,
                    or_(
                        identifiers_table.c.source_id_normalized.in_(combined_norms),
                        identifiers_table.c.source_id.in_(list(clean_vals)),
                    ),
                )
            )

        if not conds:
            return results

        stmt = select(
            identifiers_table.c.source,
            identifiers_table.c.source_id,
            identifiers_table.c.source_id_normalized,
            identifiers_table.c.entity_type,
            identifiers_table.c.entity_id,
        ).where(or_(*conds))

        allowed = set(self.authoritative_sources()) if authoritative_only else None
        if not allowed:
            allowed = None

        with self.get_read_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()

            valid_candidates: set[tuple[str, int]] = set()
            if allowed is not None and rows:
                table_map = {
                    "work": works_table,
                    "works": works_table,
                    "expression": expressions_table,
                    "expressions": expressions_table,
                    "manifestation": manifestations_table,
                    "manifestations": manifestations_table,
                    "item": items_table,
                    "items": items_table,
                    "agent": agents_table,
                    "agents": agents_table,
                }
                for etype, tbl in table_map.items():
                    eids = [r["entity_id"] for r in rows if r["entity_type"] == etype]
                    if eids:
                        check_stmt = select(tbl.c.id, tbl.c.record_origin).where(tbl.c.id.in_(eids))
                        for sr in conn.execute(check_stmt).fetchall():
                            if sr[1] in allowed:
                                valid_candidates.add((etype, sr[0]))

        for r in rows:
            etype = r["entity_type"]
            eid = r["entity_id"]
            if allowed is not None and (etype, eid) not in valid_candidates:
                continue

            src = r["source"]
            s_id = r["source_id"]
            s_norm = r["source_id_normalized"]

            candidates_for_src = source_map.get(src, [])
            for val_clean, norm_val, alnum_val, orig_pair in candidates_for_src:
                if s_norm in (norm_val, alnum_val) or s_id == val_clean:
                    if (etype, eid) not in results[orig_pair]:
                        results[orig_pair].append((etype, eid))

        for pair in needed_pairs:
            scheme, val = pair
            cache_key = (scheme.lower().strip(), val.strip().lower(), authoritative_only)
            self._identifier_lookup_cache[cache_key] = results[pair]
            if len(self._identifier_lookup_cache) > self.entity_cache_size:
                self._identifier_lookup_cache.popitem(last=False)

        return results

    def get_mediums_of_performance(self, work_id: int) -> list[EntitySubject]:
        """Fetch medium_of_performance EntitySubject records for a given work."""
        stmt = select(entity_subjects_table).where(
            entity_subjects_table.c.entity_type == "work",
            entity_subjects_table.c.entity_id == work_id,
            entity_subjects_table.c.relation_type == "medium_of_performance",
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [EntitySubject.model_validate(dict(r)) for r in rows]

