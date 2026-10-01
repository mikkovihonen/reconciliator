"""Database loaders for agent clustering metadata, attributes, and relationships."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select

from reconciliator.database import (
    WEMIRepository,
    agent_attributes_table,
    agent_relationships_table,
    contributions_table,
    entity_subjects_table,
    identifiers_table,
)


def load_identifiers(
    repo: WEMIRepository, agent_ids: list[int], batch_size: int
) -> dict[int, list[tuple[str, str]]]:
    """Load relational identifiers for agents."""
    out: dict[int, list[tuple[str, str]]] = defaultdict(list)
    with repo.get_connection() as conn:
        for i in range(0, len(agent_ids), batch_size):
            chunk = agent_ids[i : i + batch_size]
            stmt = select(
                identifiers_table.c.entity_id,
                identifiers_table.c.source,
                identifiers_table.c.source_id,
                identifiers_table.c.source_id_normalized,
            ).where(
                identifiers_table.c.entity_type == "agent",
                identifiers_table.c.entity_id.in_(chunk),
            )
            for r in conn.execute(stmt):
                aid = r[0]
                src = str(r[1]).lower()
                raw_id = str(r[2])
                norm_id = str(r[3]) if r[3] else raw_id
                out[aid].append((src, raw_id))
                if norm_id != raw_id:
                    out[aid].append((src, norm_id))
    return out


def load_attributes(
    repo: WEMIRepository, agent_ids: list[int], batch_size: int
) -> dict[int, dict[str, list[str]]]:
    """Load intrinsic attributes (qualifier, variant_name) for agents."""
    out: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    with repo.get_connection() as conn:
        for i in range(0, len(agent_ids), batch_size):
            chunk = agent_ids[i : i + batch_size]
            stmt = select(
                agent_attributes_table.c.agent_id,
                agent_attributes_table.c.attribute_type,
                agent_attributes_table.c.value,
            ).where(agent_attributes_table.c.agent_id.in_(chunk))
            for r in conn.execute(stmt):
                out[r[0]][r[1]].append(r[2])
    return out


def load_affiliations(
    repo: WEMIRepository, agent_ids: list[int], batch_size: int
) -> dict[int, set[str]]:
    """Load affiliations from agent_relationships_table and agent_attributes_table."""
    out: dict[int, set[str]] = defaultdict(set)
    with repo.get_connection() as conn:
        for i in range(0, len(agent_ids), batch_size):
            chunk = agent_ids[i : i + batch_size]
            stmt = select(
                agent_relationships_table.c.source_agent_id,
                agent_relationships_table.c.target_name,
                agent_relationships_table.c.target_uri,
            ).where(
                agent_relationships_table.c.source_agent_id.in_(chunk),
                agent_relationships_table.c.relationship_type.in_(
                    ("affiliation", "affiliated_with", "member_of")
                ),
            )
            for r in conn.execute(stmt):
                val = r[1] or r[2]
                if val:
                    out[r[0]].add(str(val).strip())

            stmt_attr = select(
                agent_attributes_table.c.agent_id,
                agent_attributes_table.c.value,
            ).where(
                agent_attributes_table.c.agent_id.in_(chunk),
                agent_attributes_table.c.attribute_type == "affiliation",
            )
            for r in conn.execute(stmt_attr):
                if r[1]:
                    out[r[0]].add(str(r[1]).strip())
    return out


def load_agent_relationships(
    repo: WEMIRepository, agent_ids: list[int], batch_size: int
) -> list[dict[str, Any]]:
    """Load directed agent relationships where either agent is in the active set."""
    out: list[dict[str, Any]] = []
    with repo.get_connection() as conn:
        for i in range(0, len(agent_ids), batch_size):
            chunk = agent_ids[i : i + batch_size]
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
            for r in conn.execute(stmt).mappings():
                out.append(dict(r))
    return out


def load_concepts(
    repo: WEMIRepository, agent_ids: list[int], batch_size: int
) -> dict[int, set[str]]:
    """Load occupations and fields of work from entity_subjects_table."""
    out: dict[int, set[str]] = defaultdict(set)
    with repo.get_connection() as conn:
        for i in range(0, len(agent_ids), batch_size):
            chunk = agent_ids[i : i + batch_size]
            stmt = select(
                entity_subjects_table.c.entity_id,
                entity_subjects_table.c.relation_type,
                entity_subjects_table.c.concept_id,
                entity_subjects_table.c.raw_label,
            ).where(
                entity_subjects_table.c.entity_type == "agent",
                entity_subjects_table.c.entity_id.in_(chunk),
            )
            for r in conn.execute(stmt):
                val = r[2] or r[3]
                if val:
                    out[r[0]].add(str(val).strip())
    return out


def load_contributions(
    repo: WEMIRepository, agent_ids: list[int], batch_size: int
) -> tuple[dict[int, set[Any]], dict[int, set[int]], dict[int, list[str]]]:
    """Load work associations, coauthors, and roles from contributions_table."""
    works_by_agent: dict[int, set[Any]] = defaultdict(set)
    roles_by_agent: dict[int, list[str]] = defaultdict(list)
    work_to_agents: dict[Any, set[int]] = defaultdict(set)

    with repo.get_connection() as conn:
        for i in range(0, len(agent_ids), batch_size):
            chunk = agent_ids[i : i + batch_size]
            stmt = select(
                contributions_table.c.agent_id,
                contributions_table.c.entity_type,
                contributions_table.c.entity_id,
                contributions_table.c.role,
            ).where(contributions_table.c.agent_id.in_(chunk))
            for r in conn.execute(stmt):
                aid = r[0]
                etype = r[1]
                eid = r[2]
                role = r[3]
                work_key = (etype, eid)
                works_by_agent[aid].add(work_key)
                roles_by_agent[aid].append(role)
                work_to_agents[work_key].add(aid)

    coauthors_by_agent: dict[int, set[int]] = defaultdict(set)
    for work_key, co_aids in work_to_agents.items():
        if len(co_aids) > 1:
            for a in co_aids:
                for b in co_aids:
                    if a != b:
                        coauthors_by_agent[a].add(b)

    return works_by_agent, coauthors_by_agent, roles_by_agent
