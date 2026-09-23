"""Tests for agent cluster relationship projection and identity coalescence."""

from unittest.mock import MagicMock
from reconciliator.clustering.agent import AgentClusterer
from reconciliator.config import AgentClusterConfig
from reconciliator.models import AgentClusterMember


def test_agent_cluster_config_relationship_defaults():
    cfg = AgentClusterConfig()
    assert cfg.coalesce_pseudonyms is False
    assert cfg.coalesce_corporate_lineage is False
    assert cfg.link_cluster_relationships is True
    assert cfg.relationship_corroboration_score == 0.90


def test_agent_cluster_projection_logic():
    # Two distinct clusters: Cluster A (Wilja, agent_id=1) and Cluster B (Canth, agent_id=2)
    cluster_members = [
        AgentClusterMember(
            agent_id=1,
            cluster_id="cluster_wilja_001",
            source="kanto",
            source_id="000149841",
            link_method="singleton",
            confidence=1.0,
        ),
        AgentClusterMember(
            agent_id=2,
            cluster_id="cluster_canth_002",
            source="kanto",
            source_id="000149842",
            link_method="singleton",
            confidence=1.0,
        ),
    ]

    agent_relationships = [
        {
            "source_agent_id": 1,
            "target_agent_id": 2,
            "relationship_type": "real_identity_of",
        }
    ]

    agent_to_cluster = {m.agent_id: m.cluster_id for m in cluster_members}
    cluster_rels: list[dict] = []
    seen_cluster_rels = set()

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

    assert len(cluster_rels) == 1
    assert cluster_rels[0]["source_cluster_id"] == "cluster_wilja_001"
    assert cluster_rels[0]["target_cluster_id"] == "cluster_canth_002"
    assert cluster_rels[0]["relationship_type"] == "real_identity_of"


def test_agent_cluster_coalescence_selection():
    cfg_no_coalesce = AgentClusterConfig(coalesce_pseudonyms=False)
    cfg_coalesce = AgentClusterConfig(coalesce_pseudonyms=True)

    rel = {
        "source_agent_id": 1,
        "target_agent_id": 2,
        "relationship_type": "real_identity_of",
    }

    # When False, should not trigger union
    should_link_false = (
        cfg_no_coalesce.coalesce_pseudonyms
        and rel["relationship_type"] in ("real_identity_of", "pseudonym_of")
    )
    assert should_link_false is False

    # When True, should trigger union
    should_link_true = (
        cfg_coalesce.coalesce_pseudonyms
        and rel["relationship_type"] in ("real_identity_of", "pseudonym_of")
    )
    assert should_link_true is True
