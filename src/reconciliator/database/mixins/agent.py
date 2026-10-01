"""Authority agents, relational attributes, Wikidata caching, and agent cluster repository mixin."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Optional

from rapidfuzz import fuzz
from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database.ops import calculate_batch_size, insert_batch, upsert_batch
from reconciliator.database.schema import (
    agent_attributes_table,
    agent_relationships_table,
    agents_table,
    contributions_table,
    corpus_agent_cluster_attributes_table,
    corpus_agent_cluster_members_table,
    corpus_agent_clusters_table,
    corpus_agent_relationships_table,
    entity_subjects_table,
    expressions_table,
    identifiers_table,
    manifestations_table,
    works_table,
)
from reconciliator.models import (
    AgentCluster,
    AgentClusterAttribute,
    AgentClusterMember,
    AgentEntity,
    AgentRelationshipEntity,
    IdentifierEntity,
)
from reconciliator.normalizers.linguistic import fold_label


class AuthorityAgentRepositoryMixin:
    """Repository mixin for agent entity CRUD, Wikidata authors cache, and agent clustering."""

    def get_connection(self) -> Any:
        raise NotImplementedError

    def get_corpus_connection(self) -> Any:
        raise NotImplementedError

    def insert_identifiers_batch(
        self,
        identifiers: list[IdentifierEntity],
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> None:
        raise NotImplementedError

    def insert_agents_batch(
        self,
        agents: list[AgentEntity],
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> list[int]:
        """Insert or update AgentEntity records in batch with relational attribute upserts."""
        if not agents:
            return []

        deduped_rows: dict[tuple[str, str], dict[str, Any]] = {}
        attr_accum: set[tuple[tuple[str, str], str, str]] = set()
        ident_accum: dict[tuple[str, str], list[IdentifierEntity]] = {}

        for agent in agents:
            key = (agent.source, agent.source_id)
            row = {
                "source": agent.source,
                "source_id": agent.source_id,
                "record_origin": agent.record_origin,
                "name": agent.name,
                "name_normalized": agent.name_normalized,
                "surname": agent.surname,
                "dates": agent.dates,
                "birth_year": agent.birth_year,
                "death_year": agent.death_year,
                "fuller_name": agent.fuller_name,
                "is_personal": agent.is_personal,
                "is_fictional": agent.is_fictional,
            }
            if key in deduped_rows:
                existing = deduped_rows[key]
                for field, val in row.items():
                    if val is not None:
                        existing[field] = val
            else:
                deduped_rows[key] = row

            if agent.qualifiers:
                for q in agent.qualifiers:
                    if q:
                        attr_accum.add((key, "qualifier", q))
            if agent.variant_names:
                for vn in agent.variant_names:
                    if vn:
                        attr_accum.add((key, "variant_name", vn))
            if agent.affiliations:
                for aff in agent.affiliations:
                    if aff:
                        attr_accum.add((key, "affiliation", aff))
            if agent.identifiers:
                ident_accum.setdefault(key, []).extend(agent.identifiers)

        rows = list(deduped_rows.values())
        id_map: dict[tuple[str, str], int] = {}

        def _exec(c: Any) -> None:
            effective_chunk_size = chunk_size or calculate_batch_size(
                num_columns=len(agents_table.c)
            )
            for i in range(0, len(rows), effective_chunk_size):
                chunk = rows[i : i + effective_chunk_size]
                stmt = pg_insert(agents_table).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["source", "source_id"],
                    set_={
                        "record_origin": stmt.excluded.record_origin,
                        "name": stmt.excluded.name,
                        "name_normalized": stmt.excluded.name_normalized,
                        "surname": func.coalesce(stmt.excluded.surname, agents_table.c.surname),
                        "dates": func.coalesce(stmt.excluded.dates, agents_table.c.dates),
                        "birth_year": func.coalesce(
                            stmt.excluded.birth_year, agents_table.c.birth_year
                        ),
                        "death_year": func.coalesce(
                            stmt.excluded.death_year, agents_table.c.death_year
                        ),
                        "fuller_name": func.coalesce(
                            stmt.excluded.fuller_name, agents_table.c.fuller_name
                        ),
                        "is_personal": stmt.excluded.is_personal,
                        "is_fictional": stmt.excluded.is_fictional,
                    },
                ).returning(agents_table.c.id, agents_table.c.source, agents_table.c.source_id)
                res = c.execute(stmt).fetchall()
                for r in res:
                    id_map[(r[1], r[2])] = r[0]

            if attr_accum:
                attr_rows = []
                for (src, sid), atype, val in attr_accum:
                    agent_id = id_map.get((src, sid))
                    if agent_id:
                        attr_rows.append(
                            {
                                "agent_id": agent_id,
                                "attribute_type": atype,
                                "value": val,
                            }
                        )
                effective_attr_chunk_size = chunk_size or calculate_batch_size(
                    num_columns=len(agent_attributes_table.c)
                )
                for i in range(0, len(attr_rows), effective_attr_chunk_size):
                    chunk = attr_rows[i : i + effective_attr_chunk_size]
                    stmt = pg_insert(agent_attributes_table).values(chunk).on_conflict_do_nothing()
                    c.execute(stmt)

            to_insert_idents: list[IdentifierEntity] = []
            for key, idents in ident_accum.items():
                agent_id = id_map.get(key)
                if agent_id:
                    for ident in idents:
                        ident_copy = ident.model_copy() if hasattr(ident, "model_copy") else ident
                        ident_copy.entity_id = agent_id
                        ident_copy.entity_type = "agent"
                        to_insert_idents.append(ident_copy)
            if to_insert_idents:
                self.insert_identifiers_batch(to_insert_idents, conn=c, chunk_size=chunk_size)

        if conn is not None:
            _exec(conn)
        else:
            with self.get_connection() as c:
                _exec(c)

        return [id_map.get((a.source, a.source_id), 0) for a in agents]

    def insert_agent_attributes_batch(
        self,
        agent_id: int,
        attributes: list[tuple[str, str]],
        conn: Optional[Any] = None,
    ) -> None:
        """Batch insert agent attributes idempotently."""
        if not attributes:
            return
        rows = [{"agent_id": agent_id, "attribute_type": t, "value": v} for t, v in attributes]
        stmt = pg_insert(agent_attributes_table).values(rows).on_conflict_do_nothing()
        if conn is not None:
            conn.execute(stmt)
        else:
            with self.get_connection() as c:
                c.execute(stmt)

    def insert_agent_relationships_batch(
        self,
        relationships: list[AgentRelationshipEntity] | list[dict[str, Any]],
        conn: Optional[Any] = None,
        chunk_size: int = 1000,
    ) -> None:
        """Batch insert agent relationships (affiliations, pseudonyms, corporate hierarchy)."""
        if not relationships:
            return
        rows = [
            r.model_dump(exclude={"id"}, exclude_none=True) if hasattr(r, "model_dump") else r
            for r in relationships
        ]

        def _exec(c: Any) -> None:
            for i in range(0, len(rows), chunk_size):
                chunk = rows[i : i + chunk_size]
                stmt = pg_insert(agent_relationships_table).values(chunk)
                c.execute(stmt)

        if conn is not None:
            _exec(conn)
        else:
            with self.get_connection() as c:
                _exec(c)

    def insert_corpus_agent_relationships_batch(
        self,
        relationships: list[dict[str, Any]],
        conn: Optional[Any] = None,
        chunk_size: int = 1000,
    ) -> None:
        """Batch insert or ignore cluster-level agent relationships into corpus_agent_relationships_table."""
        if not relationships:
            return

        def _exec(c: Any) -> None:
            for i in range(0, len(relationships), chunk_size):
                chunk = relationships[i : i + chunk_size]
                stmt = (
                    pg_insert(corpus_agent_relationships_table)
                    .values(chunk)
                    .on_conflict_do_nothing()
                )
                c.execute(stmt)

        if conn is not None:
            _exec(conn)
        else:
            with self.get_corpus_connection() as c:
                _exec(c)

    def get_agent_relationships_map(
        self, agent_ids: list[int], conn: Optional[Any] = None
    ) -> dict[int, list[dict[str, Any]]]:
        """Retrieve all relationships involving the given agent IDs, indexed by agent ID."""
        if not agent_ids:
            return {}
        out: dict[int, list[dict[str, Any]]] = defaultdict(list)
        chunk_size = 1000
        target = conn if conn is not None else self.get_connection()
        for i in range(0, len(agent_ids), chunk_size):
            chunk = agent_ids[i : i + chunk_size]
            stmt = select(
                agent_relationships_table.c.source_agent_id,
                agent_relationships_table.c.target_agent_id,
                agent_relationships_table.c.target_name,
                agent_relationships_table.c.target_uri,
                agent_relationships_table.c.relationship_type,
            ).where(
                (agent_relationships_table.c.source_agent_id.in_(chunk))
                | (agent_relationships_table.c.target_agent_id.in_(chunk))
            )
            for r in target.execute(stmt).mappings():
                d = dict(r)
                src = d["source_agent_id"]
                tgt = d["target_agent_id"]
                if src in agent_ids:
                    out[src].append(d)
                if tgt and tgt in agent_ids:
                    out[tgt].append(d)
        return out

    def insert_agent_identifiers_batch(
        self,
        agent_id: int,
        identifiers: list[IdentifierEntity],
        conn: Optional[Any] = None,
    ) -> None:
        """Batch insert identifiers for an agent."""
        if not identifiers:
            return
        bound: list[IdentifierEntity] = []
        for ident in identifiers:
            ident.entity_id = agent_id
            ident.entity_type = "agent"
            bound.append(ident)
        self.insert_identifiers_batch(bound, conn=conn)

    def insert_wikidata_authors(
        self,
        authors: list[dict[str, Any]],
        use_synchronous_commit_off: bool = True,
    ) -> None:
        """Persist author entity metadata into agents table with relational attributes."""
        if not authors:
            return
        deduped_agent_rows: dict[tuple[str, str], dict[str, Any]] = {}
        attr_accum: dict[tuple[str, str, str], int] = {}

        for a in authors:
            qid = a.get("qid")
            if not qid:
                continue

            key = ("wikidata", qid)
            row = {
                "source": "wikidata",
                "source_id": qid,
                "record_origin": "wikidata",
                "name": a.get("name") or qid,
                "name_normalized": (a.get("name") or qid).strip().lower(),
                "surname": a.get("family_name"),
                "birth_year": a.get("birth_year"),
                "death_year": a.get("death_year"),
                "is_personal": True,
            }
            if key in deduped_agent_rows:
                existing = deduped_agent_rows[key]
                for field, val in row.items():
                    if val is not None:
                        existing[field] = val
            else:
                deduped_agent_rows[key] = row

            for occ in a.get("occupations") or []:
                attr_accum[(qid, "occupation", occ)] = (
                    attr_accum.get((qid, "occupation", occ), 0) + 1
                )
            for fow in a.get("fields_of_work") or []:
                attr_accum[(qid, "field_of_work", fow)] = (
                    attr_accum.get((qid, "field_of_work", fow), 0) + 1
                )
            for aff in a.get("affiliations") or []:
                attr_accum[(qid, "affiliation", aff)] = (
                    attr_accum.get((qid, "affiliation", aff), 0) + 1
                )
            for q in a.get("qualifiers") or []:
                attr_accum[(qid, "qualifier", q)] = attr_accum.get((qid, "qualifier", q), 0) + 1
            for vn in a.get("variant_names") or []:
                attr_accum[(qid, "variant_name", vn)] = (
                    attr_accum.get((qid, "variant_name", vn), 0) + 1
                )

        agent_rows = list(deduped_agent_rows.values())
        with self.get_connection() as conn:
            if use_synchronous_commit_off:
                conn.exec_driver_sql("SET LOCAL synchronous_commit = 'off'")

            qid_to_agent_id: dict[str, int] = {}
            if agent_rows:
                agent_chunk_size = calculate_batch_size(num_columns=len(agent_rows[0]))
                for i in range(0, len(agent_rows), agent_chunk_size):
                    chunk = agent_rows[i : i + agent_chunk_size]
                    stmt = pg_insert(agents_table).values(chunk)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[agents_table.c.source, agents_table.c.source_id],
                        set_={
                            "name": stmt.excluded.name,
                            "name_normalized": stmt.excluded.name_normalized,
                            "surname": func.coalesce(stmt.excluded.surname, agents_table.c.surname),
                            "birth_year": func.coalesce(
                                stmt.excluded.birth_year, agents_table.c.birth_year
                            ),
                            "death_year": func.coalesce(
                                stmt.excluded.death_year, agents_table.c.death_year
                            ),
                        },
                    ).returning(agents_table.c.id, agents_table.c.source_id)
                    inserted = conn.execute(stmt).fetchall()
                    for r in inserted:
                        qid_to_agent_id[r[1]] = r[0]

            if qid_to_agent_id and attr_accum:
                attr_dict: dict[tuple[int, str, str], int] = {}
                for (qid, attr_type, value), count in attr_accum.items():
                    aid = qid_to_agent_id.get(qid)
                    if aid:
                        attr_dict[(aid, attr_type, value)] = (
                            attr_dict.get((aid, attr_type, value), 0) + count
                        )

                if attr_dict:
                    attr_rows = [
                        {
                            "agent_id": aid,
                            "attribute_type": atype,
                            "value": val,
                        }
                        for (aid, atype, val) in attr_dict.keys()
                    ]
                    insert_batch(
                        bind=conn,
                        table=agent_attributes_table,
                        rows=attr_rows,
                        on_conflict_do_nothing=True,
                    )

            if qid_to_agent_id:
                ident_rows = []
                for a in authors:
                    qid = a.get("qid")
                    if not qid or qid not in qid_to_agent_id:
                        continue
                    agent_id = qid_to_agent_id[qid]
                    idents = a.get("identifiers")
                    if isinstance(idents, dict):
                        for scheme, val in idents.items():
                            if val is None:
                                continue
                            val_str = str(val).strip()
                            if not val_str:
                                continue
                            src_name = scheme.lower()
                            if src_name == "wikidata_qid":
                                src_name = "wikidata"
                            norm_val = (
                                re.sub(r"[^0-9a-zA-Z]", "", val_str).lower() or val_str.lower()
                            )
                            ident_rows.append(
                                {
                                    "entity_type": "agent",
                                    "entity_id": agent_id,
                                    "source": src_name,
                                    "source_id": val_str,
                                    "source_id_normalized": norm_val,
                                }
                            )
                if ident_rows:
                    insert_batch(
                        bind=conn,
                        table=identifiers_table,
                        rows=ident_rows,
                        on_conflict_do_nothing=True,
                    )

    def get_wikidata_authors_batch(self, qids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch cached author entities from agents + identifiers + agent_attributes by QIDs."""
        if not qids:
            return {}
        with self.get_connection() as conn:
            stmt = select(
                agents_table.c.id,
                agents_table.c.source_id,
                agents_table.c.name,
                agents_table.c.surname,
                agents_table.c.birth_year,
                agents_table.c.death_year,
            ).where(
                agents_table.c.source == "wikidata",
                agents_table.c.source_id.in_(qids),
            )
            rows = conn.execute(stmt).mappings().fetchall()

            results: dict[str, dict[str, Any]] = {}
            agent_id_map: dict[str, int] = {}

            for row in rows:
                qid = row["source_id"]
                agent_id_map[qid] = row["id"]
                results[qid] = {
                    "qid": qid,
                    "name": row["name"],
                    "family_name": row["surname"],
                    "identifiers": {},
                    "birth_year": row["birth_year"],
                    "death_year": row["death_year"],
                    "occupations": [],
                    "fields_of_work": [],
                    "affiliations": [],
                    "qualifiers": [],
                    "variant_names": [],
                }

            if agent_id_map:
                attr_stmt = select(
                    agent_attributes_table.c.agent_id,
                    agent_attributes_table.c.attribute_type,
                    agent_attributes_table.c.value,
                ).where(agent_attributes_table.c.agent_id.in_(list(agent_id_map.values())))
                attr_rows = conn.execute(attr_stmt).mappings().fetchall()
                rev_map = {aid: qid for qid, aid in agent_id_map.items()}
                for r in attr_rows:
                    qid = rev_map.get(r["agent_id"])
                    if qid and qid in results:
                        atype = r["attribute_type"]
                        val = r["value"]
                        if atype == "occupation":
                            if val not in results[qid]["occupations"]:
                                results[qid]["occupations"].append(val)
                        elif atype == "field_of_work":
                            if val not in results[qid]["fields_of_work"]:
                                results[qid]["fields_of_work"].append(val)
                        elif atype == "affiliation":
                            if val not in results[qid]["affiliations"]:
                                results[qid]["affiliations"].append(val)
                        elif atype == "qualifier":
                            if val not in results[qid]["qualifiers"]:
                                results[qid]["qualifiers"].append(val)
                        elif atype == "variant_name":
                            if val not in results[qid]["variant_names"]:
                                results[qid]["variant_names"].append(val)

            if agent_id_map:
                id_stmt = select(
                    identifiers_table.c.entity_id,
                    identifiers_table.c.source,
                    identifiers_table.c.source_id,
                ).where(
                    identifiers_table.c.entity_type == "agent",
                    identifiers_table.c.entity_id.in_(list(agent_id_map.values())),
                )
                id_rows = conn.execute(id_stmt).fetchall()
                rev_map = {aid: qid for qid, aid in agent_id_map.items()}
                for r in id_rows:
                    qid = rev_map.get(r[0])
                    if qid and qid in results:
                        results[qid]["identifiers"][r[1]] = r[2]

            return results

    def get_wikidata_author(self, qid: str) -> dict[str, Any] | None:
        """Fetch a single cached author entity from agents + identifiers by QID."""
        result = self.get_wikidata_authors_batch([qid])
        return result.get(qid)

    def insert_agent_clusters_batch(self, clusters: list[AgentCluster]) -> None:
        """Batch insert or replace agent identity clusters."""
        if not clusters:
            return
        deduped = {
            c.cluster_id: {
                "cluster_id": c.cluster_id,
                "canonical_agent_id": c.canonical_agent_id,
                "canonical_agent_source": c.canonical_agent_source,
                "canonical_agent_source_id": c.canonical_agent_source_id,
                "canonical_name": c.canonical_name,
                "canonical_surname": c.canonical_surname,
                "canonical_uri": c.canonical_uri,
                "birth_year": c.birth_year,
                "death_year": c.death_year,
                "member_count": c.member_count,
                "work_count": c.work_count,
                "manifestation_count": c.manifestation_count,
                "confidence_score": c.confidence_score,
            }
            for c in clusters
        }
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=corpus_agent_clusters_table,
            rows=list(deduped.values()),
            conflict_index_elements=["cluster_id"],
        )

    def insert_agent_cluster_members_batch(self, members: list[AgentClusterMember]) -> None:
        """Batch insert or replace agent cluster members."""
        if not members:
            return
        deduped = {
            m.agent_id: {
                "agent_id": m.agent_id,
                "cluster_id": m.cluster_id,
                "source": m.source,
                "source_id": m.source_id,
                "link_method": m.link_method,
                "confidence": m.confidence,
            }
            for m in members
        }
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=corpus_agent_cluster_members_table,
            rows=list(deduped.values()),
            conflict_index_elements=["agent_id"],
        )

    def insert_agent_cluster_attributes_batch(
        self, attributes: list[AgentClusterAttribute]
    ) -> None:
        """Batch insert or update agent cluster aggregated attributes."""
        if not attributes:
            return
        rows = [
            {
                "cluster_id": a.cluster_id,
                "attribute_type": a.attribute_type,
                "value": a.value,
                "concept_id": a.concept_id,
                "frequency": a.frequency,
                "is_canonical": a.is_canonical,
            }
            for a in attributes
        ]
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=corpus_agent_cluster_attributes_table,
            rows=rows,
            conflict_index_elements=["cluster_id", "attribute_type", "value"],
        )

    def get_agent_cluster(self, cluster_id: str) -> Optional[AgentCluster]:
        """Fetch agent cluster by cluster_id with members and aggregated attributes."""
        with self.get_corpus_connection() as conn:
            row = (
                conn.execute(
                    select(corpus_agent_clusters_table).where(
                        corpus_agent_clusters_table.c.cluster_id == cluster_id
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            cluster = AgentCluster(**dict(row))
            m_rows = (
                conn.execute(
                    select(corpus_agent_cluster_members_table).where(
                        corpus_agent_cluster_members_table.c.cluster_id == cluster_id
                    )
                )
                .mappings()
                .fetchall()
            )
            cluster.members = [AgentClusterMember(**dict(m)) for m in m_rows]
            a_rows = (
                conn.execute(
                    select(corpus_agent_cluster_attributes_table).where(
                        corpus_agent_cluster_attributes_table.c.cluster_id == cluster_id
                    )
                )
                .mappings()
                .fetchall()
            )
            cluster.attributes = [AgentClusterAttribute(**dict(a)) for a in a_rows]
            return cluster

    def get_agent_cluster_members(self, cluster_id: str) -> list[AgentClusterMember]:
        """Fetch member records of an agent cluster."""
        with self.get_corpus_connection() as conn:
            rows = (
                conn.execute(
                    select(corpus_agent_cluster_members_table).where(
                        corpus_agent_cluster_members_table.c.cluster_id == cluster_id
                    )
                )
                .mappings()
                .fetchall()
            )
            return [AgentClusterMember(**dict(r)) for r in rows]

    def get_agent_cluster_attributes(self, cluster_id: str) -> list[AgentClusterAttribute]:
        """Fetch aggregated attributes of an agent cluster."""
        with self.get_corpus_connection() as conn:
            rows = (
                conn.execute(
                    select(corpus_agent_cluster_attributes_table).where(
                        corpus_agent_cluster_attributes_table.c.cluster_id == cluster_id
                    )
                )
                .mappings()
                .fetchall()
            )
            return [AgentClusterAttribute(**dict(r)) for r in rows]

    def get_canonical_agent_uri(self, agent_id: int) -> Optional[str]:
        """Get canonical URI for an agent_id through cluster membership."""
        with self.get_corpus_connection() as conn:
            stmt = (
                select(corpus_agent_clusters_table.c.canonical_uri)
                .select_from(
                    corpus_agent_cluster_members_table.join(
                        corpus_agent_clusters_table,
                        corpus_agent_cluster_members_table.c.cluster_id
                        == corpus_agent_clusters_table.c.cluster_id,
                    )
                )
                .where(corpus_agent_cluster_members_table.c.agent_id == agent_id)
            )
            val = conn.execute(stmt).scalar_one_or_none()
            return str(val) if val is not None else None

    def get_agent_cluster_id(self, agent_id: int) -> Optional[str]:
        """Get cluster_id for an agent_id through cluster membership."""
        with self.get_corpus_connection() as conn:
            stmt = select(corpus_agent_cluster_members_table.c.cluster_id).where(
                corpus_agent_cluster_members_table.c.agent_id == agent_id
            )
            val = conn.execute(stmt).scalar_one_or_none()
            return str(val) if val is not None else None

    def get_agent_cluster_full(self, cluster_id: str) -> Optional[AgentCluster]:
        """Fetch agent cluster with all member records and aggregated attributes."""
        cluster = self.get_agent_cluster(cluster_id)
        if not cluster:
            return None
        cluster.members = self.get_agent_cluster_members(cluster_id)
        cluster.attributes = self.get_agent_cluster_attributes(cluster_id)
        return cluster

    def get_relational_disambiguation_evidence(
        self,
        candidate_agent_ids: list[int],
        target_coauthors: list[str],
        target_publisher: Optional[str] = None,
        target_classifications: Optional[list[tuple[str, str]]] = None,
    ) -> dict[int, dict[str, Any]]:
        """Execute targeted SQL queries on WEMI contributions/agents/manifestations/works.

        to check co-author graph overlap, publisher overlap, and scheme-agnostic classification overlap.
        """
        evidence: dict[int, dict[str, Any]] = {
            cid: {
                "has_coauthor_match": False,
                "matched_coauthors": [],
                "has_publisher_match": False,
                "matched_publishers": [],
                "has_classification_match": False,
                "matched_classifications": [],
            }
            for cid in candidate_agent_ids
        }
        if not candidate_agent_ids:
            return evidence

        norm_target_coauthors = {fold_label(ca) for ca in target_coauthors if ca and fold_label(ca)}
        norm_target_pub = fold_label(target_publisher) if target_publisher else ""
        norm_target_classes = {
            f"{scheme.strip().lower()}:{code.strip().lower()}"
            for code, scheme in (target_classifications or [])
            if code and scheme
        }

        with self.get_connection() as conn:
            for cid in candidate_agent_ids:
                stmt = select(
                    contributions_table.c.entity_type,
                    contributions_table.c.entity_id,
                ).where(contributions_table.c.agent_id == cid)
                contribs = conn.execute(stmt).fetchall()

                work_ids = [c[1] for c in contribs if c[0] == "work"]
                manif_ids = [c[1] for c in contribs if c[0] == "manifestation"]
                expr_ids = [c[1] for c in contribs if c[0] == "expression"]

                if work_ids:
                    manif_stmt = select(manifestations_table.c.id).where(
                        manifestations_table.c.work_id.in_(work_ids)
                    )
                    for row in conn.execute(manif_stmt):
                        if row[0] not in manif_ids:
                            manif_ids.append(row[0])
                if manif_ids:
                    manif_work_stmt = select(distinct(manifestations_table.c.work_id)).where(
                        manifestations_table.c.id.in_(manif_ids),
                        manifestations_table.c.work_id.is_not(None),
                    )
                    for row in conn.execute(manif_work_stmt):
                        if row[0] not in work_ids:
                            work_ids.append(row[0])
                if expr_ids:
                    expr_work_stmt = select(distinct(expressions_table.c.work_id)).where(
                        expressions_table.c.id.in_(expr_ids),
                        expressions_table.c.work_id.is_not(None),
                    )
                    for row in conn.execute(expr_work_stmt):
                        if row[0] not in work_ids:
                            work_ids.append(row[0])

                if norm_target_coauthors and (work_ids or manif_ids or expr_ids):
                    conds = []
                    for entity_type, ids in (
                        ("work", work_ids),
                        ("expression", expr_ids),
                        ("manifestation", manif_ids),
                    ):
                        if ids:
                            conds.append(
                                and_(
                                    contributions_table.c.entity_type == entity_type,
                                    contributions_table.c.entity_id.in_(ids),
                                )
                            )
                    if conds:
                        co_stmt = (
                            select(distinct(agents_table.c.name), agents_table.c.name_normalized)
                            .select_from(
                                contributions_table.join(
                                    agents_table,
                                    contributions_table.c.agent_id == agents_table.c.id,
                                )
                            )
                            .where(or_(*conds), contributions_table.c.agent_id != cid)
                        )
                        co_agents = conn.execute(co_stmt).mappings().fetchall()
                        for co in co_agents:
                            c_norm = fold_label(co["name_normalized"] or co["name"])
                            for t_norm in norm_target_coauthors:
                                if (
                                    t_norm in c_norm
                                    or c_norm in t_norm
                                    or fuzz.token_set_ratio(t_norm, c_norm) >= 85.0
                                ):
                                    evidence[cid]["has_coauthor_match"] = True
                                    if co["name"] not in evidence[cid]["matched_coauthors"]:
                                        evidence[cid]["matched_coauthors"].append(co["name"])

                if manif_ids and norm_target_pub:
                    pub_stmt = select(distinct(manifestations_table.c.publisher)).where(
                        manifestations_table.c.id.in_(manif_ids),
                        manifestations_table.c.publisher.is_not(None),
                        manifestations_table.c.publisher != "",
                    )
                    pubs = conn.execute(pub_stmt).scalars().all()
                    for pb in pubs:
                        p_norm = fold_label(pb)
                        if p_norm and (
                            p_norm in norm_target_pub
                            or norm_target_pub in p_norm
                            or fuzz.token_set_ratio(norm_target_pub, p_norm) >= 80.0
                        ):
                            evidence[cid]["has_publisher_match"] = True
                            if pb not in evidence[cid]["matched_publishers"]:
                                evidence[cid]["matched_publishers"].append(pb)

                if work_ids and norm_target_classes:
                    class_stmt = select(
                        distinct(works_table.c.classification), works_table.c.classification_scheme
                    ).where(
                        works_table.c.id.in_(work_ids),
                        works_table.c.classification.is_not(None),
                        works_table.c.classification != "",
                    )
                    classes = conn.execute(class_stmt).all()
                    for cl, cl_scheme in classes:
                        code = str(cl).strip().lower()
                        cl_scheme_norm = (cl_scheme or "").strip().lower()
                        for tc in norm_target_classes:
                            t_scheme, _, t_code = tc.partition(":")
                            t_scheme_norm = t_scheme.strip().lower()
                            if (
                                not t_scheme_norm
                                or not cl_scheme_norm
                                or t_scheme_norm == cl_scheme_norm
                            ):
                                if code.startswith(t_code) or t_code.startswith(code):
                                    evidence[cid]["has_classification_match"] = True
                                    matched_str = f"{cl_scheme_norm or t_scheme_norm}:{code}"
                                    if matched_str not in evidence[cid]["matched_classifications"]:
                                        evidence[cid]["matched_classifications"].append(matched_str)

                    subj_stmt = select(
                        distinct(entity_subjects_table.c.concept_id),
                        entity_subjects_table.c.vocabulary,
                    ).where(
                        entity_subjects_table.c.entity_type == "work",
                        entity_subjects_table.c.entity_id.in_(work_ids),
                        entity_subjects_table.c.concept_id.is_not(None),
                    )
                    for code_val, scheme_val in conn.execute(subj_stmt):
                        code = str(code_val or "").strip().lower()
                        scheme = str(scheme_val or "").strip().lower()
                        if not code:
                            continue
                        for tc in norm_target_classes:
                            t_scheme, _, t_code = tc.partition(":")
                            if t_scheme == scheme:
                                if code.startswith(t_code) or t_code.startswith(code):
                                    evidence[cid]["has_classification_match"] = True
                                    matched_str = f"{scheme}:{code}"
                                    if matched_str not in evidence[cid]["matched_classifications"]:
                                        evidence[cid]["matched_classifications"].append(matched_str)

        return evidence
