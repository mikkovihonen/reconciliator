"""Unit tests for Virtual Authority File cluster identity and lifecycle management."""

from __future__ import annotations

from unittest.mock import MagicMock

from reconciliator.authority.lifecycle import ClusterIdentityManager
from reconciliator.config import VirtualAuthorityConfig


def test_pid_minting_modes():
    """Verify PID generation across different configured modes."""
    mock_repo = MagicMock()

    # UUIDv7 mode
    cfg_uuid = VirtualAuthorityConfig(pid_generator="uuidv7")
    mgr_uuid = ClusterIdentityManager(mock_repo, cfg_uuid)
    pid_uuid = mgr_uuid.mint_pid("agent")
    assert pid_uuid.startswith("va_agent_")
    assert len(pid_uuid) > 15

    # Monotonic int mode
    cfg_int = VirtualAuthorityConfig(pid_generator="monotonic_int")
    mgr_int = ClusterIdentityManager(mock_repo, cfg_int)
    pid_int_1 = mgr_int.mint_pid("work")
    pid_int_2 = mgr_int.mint_pid("work")
    assert pid_int_1 == "va_work_100001"
    assert pid_int_2 == "va_work_100002"

    # Opaque hash mode
    cfg_hash = VirtualAuthorityConfig(pid_generator="opaque_hash")
    mgr_hash = ClusterIdentityManager(mock_repo, cfg_hash)
    pid_hash_1 = mgr_hash.mint_pid("agent", seed="seed_a")
    pid_hash_2 = mgr_hash.mint_pid("agent", seed="seed_a")
    assert pid_hash_1 == pid_hash_2
    assert pid_hash_1.startswith("va_agent_")


def test_authority_status_determination():
    """Verify status calculation based on member origin presence in authoritative sources."""
    mock_repo = MagicMock()
    cfg = VirtualAuthorityConfig()
    mgr = ClusterIdentityManager(mock_repo, cfg)

    # Bibliographic unlinked origin -> provisional
    assert mgr.determine_authority_status(["helka", "local_repo"]) == "provisional"

    # Asteri / Kanto origin -> established
    assert mgr.determine_authority_status(["helka", "asteri"]) == "established"
    assert mgr.determine_authority_status(["kanto"]) == "established"
    assert mgr.determine_authority_status(["wikidata"]) == "established"


def test_access_point_formatting():
    """Verify authorized access point string synthesis according to cataloging conventions."""
    mock_repo = MagicMock()
    mgr = ClusterIdentityManager(mock_repo)

    # Agent access points
    ap1 = mgr.format_agent_access_point(
        name="Waltari, Mika", dates="1908-1979", fuller_name="Mika Toimi"
    )
    assert ap1 == "Waltari, Mika(Mika Toimi), 1908-1979"

    ap2 = mgr.format_agent_access_point(name="Virtanen, Jukka", qualifiers=["kirjailija"])
    assert ap2 == "Virtanen, Jukka (kirjailija)"

    # Work access points
    wp1 = mgr.format_work_access_point(title="Sinuhe egyptiläinen", author="Waltari, Mika")
    assert wp1 == "Waltari, Mika. Sinuhe egyptiläinen"

    wp2 = mgr.format_work_access_point(title="Seitsemän veljestä", work_type="moving_image")
    assert wp2 == "Seitsemän veljestä (moving_image)"


def test_resolve_agent_cluster_identity_mint_continuity_merge():
    """Verify state transitions: mint fresh cluster, maintain 1-to-1 PID, and merge multi-clusters."""
    mock_repo = MagicMock()
    mgr = ClusterIdentityManager(mock_repo)

    canon = {
        "id": 1,
        "source": "asteri",
        "source_id": "00001234",
        "name": "Waltari, Mika",
        "dates": "1908-1979",
    }
    member_1 = {"id": 1, "source": "asteri", "source_id": "00001234", "name": "Waltari, Mika"}
    member_2 = {"id": 2, "source": "melinda", "source_id": "999888", "name": "Mika Waltari"}

    # 1. Fresh Mint (no previous associations)
    cid_mint, status_mint, ap_mint, events_mint, redirs_mint = mgr.resolve_agent_cluster_identity(
        canonical_agent=canon,
        member_agents=[member_1, member_2],
        existing_cluster_map={},
    )
    assert cid_mint.startswith("va_agent_")
    assert status_mint == "established"
    assert "Waltari, Mika" in ap_mint
    assert len(events_mint) == 1
    assert events_mint[0].event_type == "mint"
    assert len(redirs_mint) == 0

    # 2. 1-to-1 Continuity (members previously in same cluster)
    existing_map = {1: cid_mint, 2: cid_mint}
    cid_cont, status_cont, ap_cont, events_cont, redirs_cont = mgr.resolve_agent_cluster_identity(
        canonical_agent=canon,
        member_agents=[member_1, member_2],
        existing_cluster_map=existing_map,
    )
    assert cid_cont == cid_mint
    assert len(events_cont) == 0
    assert len(redirs_cont) == 0

    # 3. Many-to-1 Merge (members previously in distinct clusters C_A and C_B)
    existing_map_split = {1: "va_agent_old_1", 2: "va_agent_old_2"}
    cid_merge, status_merge, ap_merge, events_merge, redirs_merge = (
        mgr.resolve_agent_cluster_identity(
            canonical_agent=canon,
            member_agents=[member_1, member_2],
            existing_cluster_map=existing_map_split,
        )
    )
    assert cid_merge == "va_agent_old_1"  # elected primary
    assert len(redirs_merge) == 1
    assert redirs_merge[0].source_cluster_id == "va_agent_old_2"
    assert redirs_merge[0].target_cluster_id == "va_agent_old_1"
    assert len(events_merge) == 1
    assert events_merge[0].event_type == "merge"
