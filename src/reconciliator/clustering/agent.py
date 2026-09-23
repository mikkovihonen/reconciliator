"""Agent clustering and co-reference resolution engine across multiple sources.

Resolves personal and corporate agent entities across Asteri, Melinda, Wikidata,
and other sources into canonical agent clusters with empirical attribute frequency
aggregation.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Any, Optional

from rapidfuzz import fuzz, utils
from sqlalchemy import select

from reconciliator.authority.lifecycle import ClusterIdentityManager
from reconciliator.config import AgentClusterConfig, AppConfig
from reconciliator.database import (
    WEMIRepository,
    agent_attributes_table,
    agent_relationships_table,
    agents_table,
    contributions_table,
    entity_subjects_table,
    identifiers_table,
)
from reconciliator.models import (
    AgentCluster,
    AgentClusterAttribute,
    AgentClusterMember,
    AgentClusterResult,
    ClusterLifecycleEvent,
    ClusterRedirection,
)
from reconciliator.normalizers.linguistic import fold_diacritics
from reconciliator.utils import UnionFind

logger = logging.getLogger(__name__)


class AgentClusterer:
    """Cross-source agent entity clustering engine."""

    def __init__(
        self,
        repo: WEMIRepository,
        config: Optional[AgentClusterConfig] = None,
        hierarchy_service: Optional[Any] = None,
        identity_manager: Optional[ClusterIdentityManager] = None,
    ) -> None:
        self.repo = repo
        if config is not None:
            self.config = config
        else:
            self.config = AppConfig().agent_clustering
        self.hierarchy_service = hierarchy_service
        self.identity_manager = identity_manager or ClusterIdentityManager(repo)

    def cluster_agents(self) -> AgentClusterResult:
        """Execute full agent clustering pipeline across sources."""
        if not self.config.enabled:
            logger.info("Agent clustering is disabled in configuration.")
            return AgentClusterResult()

        logger.info("Starting agent clustering...")

        # 1. Fetch all agents from database
        with self.repo.get_connection() as conn:
            stmt = select(
                agents_table.c.id,
                agents_table.c.source,
                agents_table.c.source_id,
                agents_table.c.name,
                agents_table.c.name_normalized,
                agents_table.c.surname,
                agents_table.c.dates,
                agents_table.c.birth_year,
                agents_table.c.death_year,
                agents_table.c.is_personal,
            )
            agent_rows = conn.execute(stmt).mappings().fetchall()

        if not agent_rows:
            logger.info("No agent records found for clustering.")
            return AgentClusterResult()

        agents: dict[int, dict[str, Any]] = {r["id"]: dict(r) for r in agent_rows}
        for a in agents.values():
            if not a.get("name_normalized") and a.get("name"):
                a["name_normalized"] = a["name"].lower()

        total_agents = len(agents)
        if total_agents == 0:
            logger.info("No agents found in database to cluster.")
            return AgentClusterResult()

        all_agent_ids = list(agents.keys())
        all_agent_id_set = set(all_agent_ids)

        # 2. Bulk load agent metadata (identifiers, attributes, relationships, concepts, works)
        logger.info("Loading agent identifiers, attributes, and relationships...")
        identifiers_by_agent = self._load_identifiers(all_agent_ids)
        attributes_by_agent = self._load_attributes(all_agent_ids)
        affiliations_by_agent = self._load_affiliations(all_agent_ids)
        concepts_by_agent = self._load_concepts(all_agent_ids)
        works_by_agent, coauthors_by_agent, roles_by_agent = self._load_contributions(all_agent_ids)
        agent_relationships = self._load_agent_relationships(all_agent_ids)

        # 3. Initialize UnionFind Disjoint-Set Structure
        uf: UnionFind[int] = UnionFind(all_agent_ids)
        link_methods: dict[tuple[int, int], str] = {}
        link_confidences: dict[tuple[int, int], float] = {}

        # 4. Stage 1: Deterministic Linking via Standard Identifiers
        self._link_by_identifiers(
            all_agent_ids, identifiers_by_agent, uf, link_methods, link_confidences
        )

        # Stage 1b: Optional Deterministic Linking via Configured Identity Coalescence
        for rel in agent_relationships:
            src_id = rel.get("source_agent_id")
            tgt_id = rel.get("target_agent_id")
            rtype = rel.get("relationship_type")
            if not src_id or not tgt_id or src_id == tgt_id:
                continue
            if src_id not in all_agent_id_set or tgt_id not in all_agent_id_set:
                continue

            should_link = False
            if self.config.coalesce_pseudonyms and rtype in (
                "real_identity_of",
                "pseudonym_of",
                "alternate_identity_of",
            ):
                should_link = True
            elif self.config.coalesce_corporate_lineage and rtype in (
                "predecessor_of",
                "successor_of",
            ):
                should_link = True

            if should_link:
                uf.union(src_id, tgt_id)
                pair_key = (min(src_id, tgt_id), max(src_id, tgt_id))
                link_methods[pair_key] = f"coalesce_{rtype}"
                link_confidences[pair_key] = 1.0

        # 5. Stage 2: Multi-Key Candidate Generation
        candidate_pairs = self._generate_blocking_candidates(agents, attributes_by_agent)
        logger.info("Generated %d candidate pairs for scoring.", len(candidate_pairs))

        # 5. Stage 3: Tolerant Scoring & Corroboration
        for aid1, aid2 in candidate_pairs:
            # If already linked by strong identifier, skip scoring
            if uf.find(aid1) == uf.find(aid2):
                continue

            a1 = agents[aid1]
            a2 = agents[aid2]

            score, method = self._score_pair(
                a1=a1,
                a2=a2,
                attrs1=attributes_by_agent.get(aid1, {}),
                attrs2=attributes_by_agent.get(aid2, {}),
                affils1=affiliations_by_agent.get(aid1, set()),
                affils2=affiliations_by_agent.get(aid2, set()),
                concepts1=concepts_by_agent.get(aid1, set()),
                concepts2=concepts_by_agent.get(aid2, set()),
                works1=works_by_agent.get(aid1, set()),
                works2=works_by_agent.get(aid2, set()),
                coauthors1=coauthors_by_agent.get(aid1, set()),
                coauthors2=coauthors_by_agent.get(aid2, set()),
            )

            if score >= self.config.similarity_threshold:
                uf.union(aid1, aid2)
                pair_key = (min(aid1, aid2), max(aid1, aid2))
                link_methods[pair_key] = method
                link_confidences[pair_key] = score

        # 6. Stage 4: Disjoint Set Partitioning
        components = uf.get_components(all_agent_id_set)

        # 7. Stage 5: Canonical Hoisting & Attribute Aggregation
        clusters: list[AgentCluster] = []
        cluster_members: list[AgentClusterMember] = []
        cluster_attributes: list[AgentClusterAttribute] = []
        all_lifecycle_events: list[ClusterLifecycleEvent] = []
        all_redirections: list[ClusterRedirection] = []
        method_counts: dict[str, int] = defaultdict(int)

        existing_cluster_map: dict[int, str] = {}
        try:
            with self.repo.get_corpus_connection() as conn:
                from reconciliator.database.schema import corpus_agent_cluster_members_table

                stmt_prev = select(
                    corpus_agent_cluster_members_table.c.agent_id,
                    corpus_agent_cluster_members_table.c.cluster_id,
                )
                for row in conn.execute(stmt_prev).mappings():
                    existing_cluster_map[row["agent_id"]] = row["cluster_id"]
        except Exception as e:
            logger.debug("No existing agent cluster mapping found: %s", e)

        singletons = 0
        multi_member = 0
        max_size = 0

        for root, member_ids in components.items():
            cluster_size = len(member_ids)
            if cluster_size == 1:
                singletons += 1
            else:
                multi_member += 1
            if cluster_size > max_size:
                max_size = cluster_size

            # Elect canonical agent using source precedence
            sorted_members = sorted(
                member_ids,
                key=lambda aid: self._authority_rank(agents[aid]),
            )
            canonical_aid = sorted_members[0]
            canonical_agent = agents[canonical_aid]

            member_agent_dicts = [agents[aid] for aid in member_ids]
            (
                cluster_id,
                auth_status,
                access_point,
                events,
                redirections,
            ) = self.identity_manager.resolve_agent_cluster_identity(
                canonical_agent=canonical_agent,
                member_agents=member_agent_dicts,
                existing_cluster_map=existing_cluster_map,
            )
            all_lifecycle_events.extend(events)
            all_redirections.extend(redirections)

            # Canonical URI: prefer override identifier from members in authority order
            canonical_uri = None
            for scheme in self.config.override_identifiers:
                for m_id in sorted_members:
                    if m_id in identifiers_by_agent:
                        for s, val in identifiers_by_agent[m_id]:
                            if s.lower() == scheme.lower():
                                canonical_uri = val
                                break
                    if canonical_uri:
                        break
                if canonical_uri:
                    break

            if not canonical_uri:
                canonical_uri = self.identity_manager.format_canonical_uri("agent", cluster_id)

            # Tally works and manifestations
            all_cluster_works: set[Any] = set()
            for m_id in member_ids:
                all_cluster_works.update(works_by_agent.get(m_id, set()))

            cluster = AgentCluster(
                cluster_id=cluster_id,
                canonical_agent_id=canonical_aid,
                canonical_agent_source=canonical_agent["source"],
                canonical_agent_source_id=canonical_agent["source_id"],
                canonical_name=canonical_agent["name"],
                canonical_surname=canonical_agent.get("surname"),
                canonical_uri=canonical_uri,
                birth_year=canonical_agent.get("birth_year"),
                death_year=canonical_agent.get("death_year"),
                member_count=cluster_size,
                work_count=len(all_cluster_works),
                manifestation_count=len(all_cluster_works),
                confidence_score=1.0,
                authority_status=auth_status,
                canonical_access_point=access_point,
            )
            clusters.append(cluster)

            # Build member records
            for m_id in member_ids:
                m_agent = agents[m_id]
                if m_id == canonical_aid:
                    link_method = "canonical_seed"
                    conf = 1.0
                else:
                    pair_key = (min(m_id, canonical_aid), max(m_id, canonical_aid))
                    link_method = link_methods.get(pair_key, "cluster_transitive")
                    conf = link_confidences.get(pair_key, 0.90)

                method_counts[link_method] += 1
                cluster_members.append(
                    AgentClusterMember(
                        agent_id=m_id,
                        cluster_id=cluster_id,
                        source=m_agent["source"],
                        source_id=m_agent["source_id"],
                        link_method=link_method,
                        confidence=conf,
                    )
                )

            # Build cluster attributes with frequency
            name_counts: Counter[str] = Counter()
            qual_counts: Counter[str] = Counter()
            affil_counts: Counter[str] = Counter()
            concept_counts: Counter[str] = Counter()
            role_counts: Counter[str] = Counter()

            for m_id in member_ids:
                m_agent = agents[m_id]
                name_counts[m_agent["name"]] += 1
                for vn in attributes_by_agent.get(m_id, {}).get("variant_name", []):
                    name_counts[vn] += 1
                for q in attributes_by_agent.get(m_id, {}).get("qualifier", []):
                    qual_counts[q] += 1
                for aff in affiliations_by_agent.get(m_id, set()):
                    affil_counts[aff] += 1
                for c_val in concepts_by_agent.get(m_id, set()):
                    concept_counts[c_val] += 1
                for r_val in roles_by_agent.get(m_id, []):
                    role_counts[r_val] += 1

            # Variant names
            canonical_name_str = canonical_agent["name"]
            for val, freq in name_counts.items():
                cluster_attributes.append(
                    AgentClusterAttribute(
                        cluster_id=cluster_id,
                        attribute_type="variant_name",
                        value=val,
                        frequency=freq,
                        is_canonical=(val == canonical_name_str),
                    )
                )

            # Qualifiers
            if qual_counts:
                most_common_qual = qual_counts.most_common(1)[0][0]
                for val, freq in qual_counts.items():
                    cluster_attributes.append(
                        AgentClusterAttribute(
                            cluster_id=cluster_id,
                            attribute_type="qualifier",
                            value=val,
                            frequency=freq,
                            is_canonical=(val == most_common_qual),
                        )
                    )

            # Affiliations
            if affil_counts:
                most_common_aff = affil_counts.most_common(1)[0][0]
                for val, freq in affil_counts.items():
                    cluster_attributes.append(
                        AgentClusterAttribute(
                            cluster_id=cluster_id,
                            attribute_type="affiliation",
                            value=val,
                            frequency=freq,
                            is_canonical=(val == most_common_aff),
                        )
                    )

            # Occupations / Concepts
            if concept_counts:
                most_common_concept = concept_counts.most_common(1)[0][0]
                for val, freq in concept_counts.items():
                    cluster_attributes.append(
                        AgentClusterAttribute(
                            cluster_id=cluster_id,
                            attribute_type="occupation",
                            value=val,
                            frequency=freq,
                            is_canonical=(val == most_common_concept),
                        )
                    )

            # Relator Roles
            if role_counts:
                most_common_role = role_counts.most_common(1)[0][0]
                for val, freq in role_counts.items():
                    cluster_attributes.append(
                        AgentClusterAttribute(
                            cluster_id=cluster_id,
                            attribute_type="role",
                            value=val,
                            frequency=freq,
                            is_canonical=(val == most_common_role),
                        )
                    )

        # 8. Stage 6: Database Persistence (Batch)
        logger.info("Persisting %d agent clusters to database...", len(clusters))
        self.repo.insert_agent_clusters_batch(clusters)
        logger.info("Persisting %d cluster members to database...", len(cluster_members))
        self.repo.insert_agent_cluster_members_batch(cluster_members)
        logger.info("Persisting %d cluster attributes to database...", len(cluster_attributes))
        self.repo.insert_agent_cluster_attributes_batch(cluster_attributes)
        self.identity_manager.record_lifecycle_batch(all_lifecycle_events, all_redirections)

        # Stage 6b: Project agent relationships into corpus_agent_relationships_table
        if self.config.link_cluster_relationships and agent_relationships:
            agent_to_cluster = {m.agent_id: m.cluster_id for m in cluster_members}
            cluster_rels: list[dict[str, Any]] = []
            seen_cluster_rels: set[tuple[str, str, str]] = set()

            for rel in agent_relationships:
                src_id = rel.get("source_agent_id")
                tgt_id = rel.get("target_agent_id")
                rtype = rel.get("relationship_type")
                if not src_id or not tgt_id or not rtype:
                    continue
                c_src = agent_to_cluster.get(src_id)
                c_tgt = agent_to_cluster.get(tgt_id)
                if not c_src or not c_tgt or c_src == c_tgt:
                    continue
                rel_key = (c_src, c_tgt, rtype)
                if rel_key not in seen_cluster_rels:
                    seen_cluster_rels.add(rel_key)
                    cluster_rels.append(
                        {
                            "source_cluster_id": c_src,
                            "target_cluster_id": c_tgt,
                            "relationship_type": rtype,
                            "confidence": 1.0,
                        }
                    )
            if cluster_rels:
                logger.info(
                    "Persisting %d inter-cluster agent relationships to corpus_agent_relationships...",
                    len(cluster_rels),
                )
                self.repo.insert_corpus_agent_relationships_batch(cluster_rels)

        logger.info(
            "Agent clustering complete: %d total agents partitioned into %d clusters (%d multi-member, %d singletons).",
            total_agents,
            len(clusters),
            multi_member,
            singletons,
        )

        result = AgentClusterResult(
            total_agents=total_agents,
            total_clusters=len(clusters),
            multi_member_clusters=multi_member,
            singleton_clusters=singletons,
            max_cluster_size=max_size,
            link_methods_breakdown=dict(method_counts),
        )
        return result

    def _authority_rank(self, agent: dict[str, Any]) -> tuple[int, int, str]:
        """Rank agent for canonical representative election based on configured source precedence."""
        src = str(agent.get("source", "")).lower()
        try:
            rank = self.config.source_authority_precedence.index(src)
        except ValueError:
            rank = len(self.config.source_authority_precedence) + 1
        # Secondary sort: prefer rows with birth_year, then length of name
        has_dates = 0 if agent.get("birth_year") is not None else 1
        return (rank, has_dates, agent.get("source_id", ""))

    def _load_identifiers(self, agent_ids: list[int]) -> dict[int, list[tuple[str, str]]]:
        """Load relational identifiers for agents."""
        out: dict[int, list[tuple[str, str]]] = defaultdict(list)
        chunk_size = self.config.batch_size
        with self.repo.get_connection() as conn:
            for i in range(0, len(agent_ids), chunk_size):
                chunk = agent_ids[i : i + chunk_size]
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

    def _load_attributes(self, agent_ids: list[int]) -> dict[int, dict[str, list[str]]]:
        """Load intrinsic attributes (qualifier, variant_name) for agents."""
        out: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        chunk_size = self.config.batch_size
        with self.repo.get_connection() as conn:
            for i in range(0, len(agent_ids), chunk_size):
                chunk = agent_ids[i : i + chunk_size]
                stmt = select(
                    agent_attributes_table.c.agent_id,
                    agent_attributes_table.c.attribute_type,
                    agent_attributes_table.c.value,
                ).where(agent_attributes_table.c.agent_id.in_(chunk))
                for r in conn.execute(stmt):
                    out[r[0]][r[1]].append(r[2])
        return out

    def _load_affiliations(self, agent_ids: list[int]) -> dict[int, set[str]]:
        """Load affiliations from agent_relationships_table and agent_attributes_table."""
        out: dict[int, set[str]] = defaultdict(set)
        chunk_size = self.config.batch_size
        with self.repo.get_connection() as conn:
            for i in range(0, len(agent_ids), chunk_size):
                chunk = agent_ids[i : i + chunk_size]
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

                # Also load from agent_attributes_table
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

    def _load_agent_relationships(self, agent_ids: list[int]) -> list[dict[str, Any]]:
        """Load directed agent relationships where either agent is in the active set."""
        out: list[dict[str, Any]] = []
        chunk_size = self.config.batch_size
        with self.repo.get_connection() as conn:
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
                for r in conn.execute(stmt).mappings():
                    out.append(dict(r))
        return out

    def _load_concepts(self, agent_ids: list[int]) -> dict[int, set[str]]:
        """Load occupations and fields of work from entity_subjects_table."""
        out: dict[int, set[str]] = defaultdict(set)
        chunk_size = self.config.batch_size
        with self.repo.get_connection() as conn:
            for i in range(0, len(agent_ids), chunk_size):
                chunk = agent_ids[i : i + chunk_size]
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

    def _load_contributions(
        self, agent_ids: list[int]
    ) -> tuple[dict[int, set[Any]], dict[int, set[int]], dict[int, list[str]]]:
        """Load work associations, coauthors, and roles from contributions_table."""
        works_by_agent: dict[int, set[Any]] = defaultdict(set)
        roles_by_agent: dict[int, list[str]] = defaultdict(list)
        work_to_agents: dict[Any, set[int]] = defaultdict(set)

        chunk_size = self.config.batch_size
        with self.repo.get_connection() as conn:
            for i in range(0, len(agent_ids), chunk_size):
                chunk = agent_ids[i : i + chunk_size]
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

        # Build coauthors
        coauthors_by_agent: dict[int, set[int]] = defaultdict(set)
        for work_key, co_aids in work_to_agents.items():
            if len(co_aids) > 1:
                for a in co_aids:
                    for b in co_aids:
                        if a != b:
                            coauthors_by_agent[a].add(b)

        return works_by_agent, coauthors_by_agent, roles_by_agent

    def _link_by_identifiers(
        self,
        agent_ids: list[int],
        identifiers_by_agent: dict[int, list[tuple[str, str]]],
        uf: UnionFind[int],
        link_methods: dict[tuple[int, int], str],
        link_confidences: dict[tuple[int, int], float],
    ) -> None:
        """Deterministic Stage 1 linking via standard identifiers."""
        override_schemes = {s.lower() for s in self.config.override_identifiers}
        ident_index: dict[tuple[str, str], list[int]] = defaultdict(list)

        for aid in agent_ids:
            for scheme, val in identifiers_by_agent.get(aid, []):
                norm_scheme = scheme.lower()
                if norm_scheme in override_schemes:
                    norm_val = val.replace("-", "").replace(" ", "").strip().lower()
                    if norm_val:
                        ident_index[(norm_scheme, norm_val)].append(aid)

        for (scheme, _), aids in ident_index.items():
            if len(aids) > 1:
                primary = aids[0]
                for other in aids[1:]:
                    uf.union(primary, other)
                    pair_key = (min(primary, other), max(primary, other))
                    link_methods[pair_key] = f"identifier_{scheme}"
                    link_confidences[pair_key] = 1.0

    def _generate_blocking_candidates(
        self,
        agents: dict[int, dict[str, Any]],
        attributes_by_agent: dict[int, dict[str, list[str]]],
    ) -> set[tuple[int, int]]:
        """Generate candidate pairs using configured multi-key blocking strategies."""
        candidates: set[tuple[int, int]] = set()
        blocking_keys = set(self.config.blocking_keys)

        # Buckets for each strategy
        buckets: dict[str, list[int]] = defaultdict(list)

        for aid, a in agents.items():
            # Strategy: name_normalized
            if "name_normalized" in blocking_keys:
                n_norm = a.get("name_normalized")
                if n_norm:
                    buckets[f"name:{n_norm}"].append(aid)

            # Strategy: surname_decade
            if "surname_decade" in blocking_keys:
                surname = a.get("surname")
                birth = a.get("birth_year")
                if surname and birth:
                    decade = birth // 10
                    buckets[f"sur_decade:{surname.lower()}_{decade}x"].append(aid)

            # Strategy: folded_surname
            if "folded_surname" in blocking_keys:
                surname = a.get("surname")
                if surname:
                    folded = fold_diacritics(surname).lower().strip()
                    buckets[f"folded_sur:{folded}"].append(aid)

            # Strategy: variant_names
            if "variant_names" in blocking_keys:
                for vn in attributes_by_agent.get(aid, {}).get("variant_name", []):
                    if vn:
                        buckets[f"var_name:{vn.strip().lower()}"].append(aid)

        max_bucket = self.config.max_blocking_bucket_size
        for _, aids in buckets.items():
            if 1 < len(aids) <= max_bucket:  # Protect against massive uninformative buckets
                for i in range(len(aids)):
                    for j in range(i + 1, len(aids)):
                        candidates.add((min(aids[i], aids[j]), max(aids[i], aids[j])))

        return candidates

    def _score_pair(
        self,
        a1: dict[str, Any],
        a2: dict[str, Any],
        attrs1: dict[str, list[str]],
        attrs2: dict[str, list[str]],
        affils1: set[str],
        affils2: set[str],
        concepts1: set[str],
        concepts2: set[str],
        works1: set[Any],
        works2: set[Any],
        coauthors1: set[int],
        coauthors2: set[int],
    ) -> tuple[float, str]:
        """Evaluate match score between two agents using configured weights and tolerances."""
        # 1. Check Date Discrepancies
        b1, d1 = a1.get("birth_year"), a1.get("death_year")
        b2, d2 = a2.get("birth_year"), a2.get("death_year")

        # Major Date Discrepancy Veto
        major_veto = self.config.major_date_discrepancy_veto_years
        if b1 is not None and b2 is not None and abs(b1 - b2) >= major_veto:
            return 0.0, "veto_birth_year_major_discrepancy"
        if d1 is not None and d2 is not None and abs(d1 - d2) >= major_veto:
            return 0.0, "veto_death_year_major_discrepancy"

        # Minor Date Discrepancy Corroboration Requirement
        has_corroboration = bool(
            (works1 & works2)
            or (coauthors1 & coauthors2)
            or (affils1 & affils2)
            or (concepts1 & concepts2)
        )

        has_date_diff = (b1 is not None and b2 is not None and b1 != b2) or (
            d1 is not None and d2 is not None and d1 != d2
        )
        if has_date_diff:
            max_diff = max(
                abs(b1 - b2) if b1 is not None and b2 is not None else 0,
                abs(d1 - d2) if d1 is not None and d2 is not None else 0,
            )
            if max_diff <= self.config.date_tolerance_years:
                if self.config.require_corroboration_for_date_discrepancy and not has_corroboration:
                    return 0.0, "rejected_minor_date_discrepancy_without_corroboration"
            else:
                return 0.0, "rejected_date_discrepancy_exceeds_tolerance"

        # 2. Feature Scoring
        weights = self.config.scoring_weights

        # Name score (comparing primary and variants)
        names1 = [a1["name_normalized"]] + [v.lower() for v in attrs1.get("variant_name", [])]
        names2 = [a2["name_normalized"]] + [v.lower() for v in attrs2.get("variant_name", [])]

        best_name_sim = 0.0
        for n1 in names1:
            for n2 in names2:
                if self.config.token_matching_algorithm == "token_set_ratio":
                    sim = fuzz.token_set_ratio(n1, n2, processor=utils.default_process) / 100.0
                else:
                    sim = fuzz.ratio(n1, n2, processor=utils.default_process) / 100.0
                if sim > best_name_sim:
                    best_name_sim = sim
        name_score = best_name_sim

        score_sum = weights.name_weight * name_score
        weight_sum = weights.name_weight

        # Dates score
        if b1 is not None and b2 is not None:
            if b1 == b2:
                date_score = 1.0
            elif abs(b1 - b2) <= self.config.date_tolerance_years:
                date_score = self.config.date_tolerance_score
            else:
                date_score = 0.0
        elif (b1 is None and b2 is not None) or (b1 is not None and b2 is None):
            date_score = self.config.date_missing_score  # Neutral missing date
        else:
            date_score = self.config.date_missing_score
        score_sum += weights.dates_weight * date_score
        weight_sum += weights.dates_weight

        # Work cluster score
        if works1 or works2:
            if works1 and works2:
                shared = len(works1 & works2)
                total = len(works1 | works2)
                work_score = shared / total if total else 0.0
            else:
                work_score = 0.0
            score_sum += weights.work_cluster_weight * work_score
            weight_sum += weights.work_cluster_weight

        # Coauthor score (shared third-party coauthors)
        ca1_clean = coauthors1 - {a2["id"]}
        ca2_clean = coauthors2 - {a1["id"]}
        if ca1_clean and ca2_clean:
            shared_ca = len(ca1_clean & ca2_clean)
            total_ca = len(ca1_clean | ca2_clean)
            coauthor_score = shared_ca / total_ca if total_ca else 0.0
            score_sum += weights.coauthor_weight * coauthor_score
            weight_sum += weights.coauthor_weight

        # Affiliation score
        if affils1 and affils2:
            if affils1 & affils2:
                affiliation_score = 1.0
            else:
                # Fuzzy overlap
                affiliation_score = 0.0
                for af1 in affils1:
                    for af2 in affils2:
                        if (
                            fuzz.token_set_ratio(af1.lower(), af2.lower())
                            >= self.config.affiliation_fuzzy_threshold
                        ):
                            affiliation_score = self.config.affiliation_fuzzy_score
                            break
                    if affiliation_score > 0:
                        break
            score_sum += weights.affiliation_weight * affiliation_score
            weight_sum += weights.affiliation_weight

        # Concept (occupation/field_of_work) score
        if concepts1 and concepts2:
            if concepts1 & concepts2:
                concept_score = 1.0
            elif (
                getattr(self.config, "enable_hierarchical_concepts", True)
                and self.hierarchy_service is not None
            ):
                sim = self.hierarchy_service.concepts_similarity(concepts1, concepts2)
                concept_score = sim * getattr(self.config, "concept_similarity_weight", 0.85)
            else:
                concept_score = 0.0
            score_sum += weights.concept_weight * concept_score
            weight_sum += weights.concept_weight

        # Qualifier score
        q1 = set(attrs1.get("qualifier", []))
        q2 = set(attrs2.get("qualifier", []))
        if q1 and q2:
            qualifier_score = 1.0 if (q1 & q2) else 0.0
            score_sum += weights.qualifier_weight * qualifier_score
            weight_sum += weights.qualifier_weight

        composite_score = score_sum / weight_sum if weight_sum > 0 else 0.0

        return composite_score, "weighted_scoring"
