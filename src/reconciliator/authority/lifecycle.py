"""Persistent identity and lifecycle management for Virtual Authority File clusters.

Handles time-ordered PID generation, cluster merge and split detection, authority status
assignment (established vs provisional), canonical access point formatting, and lifecycle
ledger event recording.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Optional

from reconciliator.config import AppConfig, VirtualAuthorityConfig
from reconciliator.database import WEMIRepository
from reconciliator.models import ClusterLifecycleEvent, ClusterRedirection

logger = logging.getLogger(__name__)


class ClusterIdentityManager:
    """Manages persistent identifiers, merge/split state transitions, and authority ledgers."""

    def __init__(
        self,
        repo: WEMIRepository,
        config: Optional[VirtualAuthorityConfig] = None,
    ) -> None:
        self.repo = repo
        if config is not None:
            self.config = config
        else:
            self.config = AppConfig().virtual_authority
        self._monotonic_counters: dict[str, int] = {}

    def mint_pid(self, cluster_type: str, seed: Optional[str] = None) -> str:
        """Mint a persistent identifier based on the configured pid_generator."""
        generator_mode = self.config.pid_generator.lower()
        if generator_mode == "uuidv7" and hasattr(uuid, "uuid7"):
            raw_id = str(uuid.uuid7())
        elif generator_mode == "monotonic_int":
            current = self._monotonic_counters.get(cluster_type, 100000) + 1
            self._monotonic_counters[cluster_type] = current
            raw_id = str(current)
        else:
            # Fallback to opaque hash
            hash_source = seed or str(uuid.uuid4())
            raw_id = hashlib.sha256(hash_source.encode("utf-8")).hexdigest()[:16]

        prefix = f"va_{cluster_type}_"
        return f"{prefix}{raw_id}"

    def format_canonical_uri(self, cluster_type: str, cluster_id: str) -> str:
        """Format the full canonical URI based on configured URI templates."""
        template = self.config.uri_templates.get(cluster_type)
        if template:
            return template.format(id=cluster_id)
        return f"{self.config.base_uri}{cluster_type}:{cluster_id}"

    def determine_authority_status(self, member_origins: list[str]) -> str:
        """Determine if a cluster is established or provisional based on member record origins."""
        authoritative = {s.lower() for s in self.config.status_policy.authoritative_origins}
        for origin in member_origins:
            if origin and origin.lower() in authoritative:
                return self.config.status_policy.established_status_name
        return self.config.status_policy.provisional_status_name

    def format_agent_access_point(
        self,
        name: str,
        dates: Optional[str] = None,
        fuller_name: Optional[str] = None,
        qualifiers: Optional[list[str]] = None,
    ) -> str:
        """Format authorized access point for an agent heading according to cataloging rules."""
        heading_parts = [name.strip()]
        if fuller_name and fuller_name.strip():
            heading_parts.append(f"({fuller_name.strip()})")
        if dates and dates.strip():
            heading_parts.append(f", {dates.strip()}")
        elif qualifiers:
            heading_parts.append(f" ({qualifiers[0].strip()})")
        return "".join(heading_parts)

    def format_work_access_point(
        self,
        title: str,
        author: Optional[str] = None,
        work_type: Optional[str] = None,
    ) -> str:
        """Format authorized access point for a work uniform title heading."""
        if author and author.strip():
            return f"{author.strip()}. {title.strip()}"
        if work_type and work_type not in ("text", "monograph"):
            return f"{title.strip()} ({work_type.strip()})"
        return title.strip()

    def resolve_agent_cluster_identity(
        self,
        canonical_agent: dict[str, Any],
        member_agents: list[dict[str, Any]],
        existing_cluster_map: Optional[dict[int, str]] = None,
    ) -> tuple[str, str, str, list[ClusterLifecycleEvent], list[ClusterRedirection]]:
        """Resolve persistent cluster ID, status, access point, and lifecycle events for an agent cluster.

        Returns (cluster_id, authority_status, canonical_access_point, lifecycle_events, redirections).
        """
        existing_map = existing_cluster_map or {}
        events: list[ClusterLifecycleEvent] = []
        redirections: list[ClusterRedirection] = []

        member_ids = [m["id"] for m in member_agents if "id" in m]
        member_origins = [
            str(m.get("source") or m.get("record_origin") or "") for m in member_agents
        ]
        status = self.determine_authority_status(member_origins)

        # Detect previous cluster associations of participating members
        previous_clusters: set[str] = {
            existing_map[mid] for mid in member_ids if mid in existing_map
        }

        if not previous_clusters:
            # Entirely new cluster: mint fresh PID
            seed = f"{canonical_agent.get('source')}:{canonical_agent.get('source_id')}:{canonical_agent.get('name')}"
            cluster_id = self.mint_pid("agent", seed=seed)
            events.append(
                ClusterLifecycleEvent(
                    event_type="mint",
                    cluster_type="agent",
                    cluster_id=cluster_id,
                    details_json=json.dumps(
                        {
                            "canonical_source": canonical_agent.get("source"),
                            "canonical_source_id": canonical_agent.get("source_id"),
                            "member_count": len(member_agents),
                        }
                    ),
                )
            )
        elif len(previous_clusters) == 1:
            # 1-to-1 continuity: preserve established PID
            cluster_id = next(iter(previous_clusters))
        else:
            # Many-to-1 merge: elect primary cluster ID from previous clusters
            sorted_previous = sorted(list(previous_clusters))
            cluster_id = sorted_previous[0]
            for superseded_id in sorted_previous[1:]:
                redirections.append(
                    ClusterRedirection(
                        source_cluster_id=superseded_id,
                        target_cluster_id=cluster_id,
                        cluster_type="agent",
                        status_code=self.config.lifecycle.redirect_status_code,
                        reason="merge",
                    )
                )
                events.append(
                    ClusterLifecycleEvent(
                        event_type="merge",
                        cluster_type="agent",
                        cluster_id=superseded_id,
                        target_cluster_id=cluster_id,
                        details_json=json.dumps(
                            {
                                "merged_into": cluster_id,
                                "superseded_id": superseded_id,
                                "member_count": len(member_agents),
                            }
                        ),
                    )
                )

        access_point = self.format_agent_access_point(
            name=str(canonical_agent.get("name") or ""),
            dates=canonical_agent.get("dates"),
            fuller_name=canonical_agent.get("fuller_name"),
        )
        return cluster_id, status, access_point, events, redirections

    def resolve_work_cluster_identity(
        self,
        canonical_title: str,
        canonical_author: Optional[str] = None,
        work_type: str = "text",
        member_record_ids: Optional[list[str]] = None,
        existing_cluster_map: Optional[dict[str, str]] = None,
    ) -> tuple[str, str, str, list[ClusterLifecycleEvent], list[ClusterRedirection]]:
        """Resolve persistent cluster ID, status, access point, and lifecycle events for a work cluster."""
        existing_map = existing_cluster_map or {}
        records = member_record_ids or []
        events: list[ClusterLifecycleEvent] = []
        redirections: list[ClusterRedirection] = []

        previous_clusters = {existing_map[rid] for rid in records if rid in existing_map}

        if not previous_clusters:
            seed = f"{canonical_title}::{canonical_author or ''}::{work_type}"
            cluster_id = self.mint_pid("work", seed=seed)
            events.append(
                ClusterLifecycleEvent(
                    event_type="mint",
                    cluster_type="work",
                    cluster_id=cluster_id,
                    details_json=json.dumps(
                        {
                            "title": canonical_title,
                            "author": canonical_author,
                            "records_count": len(records),
                        }
                    ),
                )
            )
        elif len(previous_clusters) == 1:
            cluster_id = next(iter(previous_clusters))
        else:
            sorted_previous = sorted(list(previous_clusters))
            cluster_id = sorted_previous[0]
            for superseded_id in sorted_previous[1:]:
                redirections.append(
                    ClusterRedirection(
                        source_cluster_id=superseded_id,
                        target_cluster_id=cluster_id,
                        cluster_type="work",
                        status_code=self.config.lifecycle.redirect_status_code,
                        reason="merge",
                    )
                )
                events.append(
                    ClusterLifecycleEvent(
                        event_type="merge",
                        cluster_type="work",
                        cluster_id=superseded_id,
                        target_cluster_id=cluster_id,
                        details_json=json.dumps(
                            {
                                "merged_into": cluster_id,
                                "superseded_id": superseded_id,
                            }
                        ),
                    )
                )

        access_point = self.format_work_access_point(
            title=canonical_title,
            author=canonical_author,
            work_type=work_type,
        )
        # For works, if canonical author or records have authority provenance, mark established
        status = "provisional"
        if canonical_author:
            status = "established"

        return cluster_id, status, access_point, events, redirections

    def record_lifecycle_batch(
        self,
        events: list[ClusterLifecycleEvent],
        redirections: list[ClusterRedirection],
    ) -> None:
        """Persist accumulated lifecycle events and redirections to the database repository."""
        if events:
            self.repo.insert_cluster_lifecycle_events_batch(events)
        if redirections:
            self.repo.insert_cluster_redirections_batch(redirections)
