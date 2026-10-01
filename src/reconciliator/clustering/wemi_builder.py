"""Shared WEMI cluster entity builder, RDA inference, and persistence helpers.

Consolidates WEMI entity assembly and database persistence across both
continuous (BigClust) and discrete graph (MultiplexGraph) clustering engines.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from typing import Any, Optional

from reconciliator.authority.lifecycle import ClusterIdentityManager
from reconciliator.clustering.common import CanonicalRepresentativeSelector
from reconciliator.database import WEMIRepository
from reconciliator.models import (
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    MatchRecord,
    WorkCluster,
    WorkType,
)
from reconciliator.normalizers.blocking import compute_author_key, compute_title_stem

logger = logging.getLogger(__name__)


def infer_rda_content_type(target: MatchRecord, music_cfg: Any = None) -> str:
    """Infer standard RDA content type from MatchRecord work_type and metadata.

    Args:
        target: MatchRecord instance.
        music_cfg: Optional music score configuration object (e.g. config.music_score).

    Returns:
        Canonical RDA content type string (e.g., 'text', 'spoken_word', 'notated_music').
    """
    wt = target.work_type
    if wt == "audio_nonmusic":
        return "spoken_word"
    elif wt == "music":
        is_score = False
        if music_cfg:
            score_terms = tuple(t.lower() for t in getattr(music_cfg, "terms", ()))
            title_text = (target.title_stem or "").lower()
            vol_text = (target.volume_designation or "").lower()
            is_score = any(t in title_text for t in score_terms) or any(
                t in vol_text for t in score_terms
            )
            standard_key = target.get_first_key("standard")
            std_terms = getattr(music_cfg, "standard_key_terms", ())
            if standard_key and any(x.lower() in standard_key.lower() for x in std_terms):
                is_score = True
        return "notated_music" if is_score else "audio_music"
    elif wt == "moving_image":
        return "moving_image"
    elif wt == "cartography":
        return "cartographic_image"
    elif wt == "tactile":
        return "tactile_text"
    elif wt == "software":
        return "computer_program"
    elif wt == "ai_model":
        return "model_weights"
    elif wt in ("board_game", "realia"):
        return "three_dimensional"
    elif wt in ("dataset", "geodata"):
        return "dataset"
    elif wt == "still_image":
        return "still_image"
    elif wt in ("podcast", "oral_history"):
        return "spoken_word"
    return "text"


def extract_expression_partition_keys(
    target: MatchRecord,
    music_cfg: Any = None,
) -> tuple[str, str, str]:
    """Extract (content_type, language, sub_key) partition signature for expression sub-clustering.

    Args:
        target: MatchRecord instance.
        music_cfg: Optional music score configuration object.

    Returns:
        Tuple of (content_type, language, sub_key).
    """
    content_type = infer_rda_content_type(target, music_cfg=music_cfg)
    lang = (target.language or "und").strip().lower()

    if content_type == "text":
        sub_key = (target.get_first_key("translators_signature") or "").strip().lower()
    elif content_type == "spoken_word":
        sub_key = (
            (target.get_first_key("translators_signature") or target.author_key or "")
            .strip()
            .lower()
        )
    elif content_type == "audio_music":
        nums = re.findall(r"\b\d+\b", target.title_stem or target.title or "")
        num_suffix = f"_{'_'.join(nums)}" if nums else ""
        sub_key = f"{(target.author_key or '').strip().lower()}_{target.year or ''}{num_suffix}"
    elif content_type == "notated_music":
        if target.volume_designation:
            sub_key = target.volume_designation.strip().lower()
        else:
            nums = re.findall(r"\b\d+\b", target.title_stem or target.title or "")
            sub_key = f"score_{'_'.join(nums)}" if nums else "score"
    elif content_type in ("computer_program", "model_weights", "dataset"):
        sub_key = (target.volume_designation or "v1").strip().lower()
    elif content_type == "three_dimensional":
        sub_key = (target.volume_designation or "").strip().lower()
    else:
        sub_key = (target.volume_designation or "").strip().lower()

    return content_type, lang, sub_key


def extract_manifestation_key(target: MatchRecord) -> str:
    """Extract carrier publication signature for manifestation duplicate clustering."""
    sk = (target.get_first_key("standard") or "").strip().lower()
    year = str(target.year or 0)
    vol = (target.volume_designation or "").strip().lower()
    if sk:
        return f"{sk}::{year}::{vol}"
    t_stem = (target.title_stem or target.title or "").strip().lower()
    return f"raw::{target.record_id}::{t_stem}::{year}::{vol}"


_work_canonical_selector = CanonicalRepresentativeSelector[MatchRecord](
    tie_breaker=lambda r: (
        -(1 if r.uniform_title else 0),
        -(len(r.title or "")),
        r.record_id,
    )
)


def select_canonical_work_record(records: Sequence[MatchRecord]) -> MatchRecord:
    """Select the best representative MatchRecord for a Work cluster.

    Prefers uniform titles, longer display titles, and deterministic record_id sorting.
    """
    return _work_canonical_selector.select_canonical(records)


class WEMIClusterBuilder:
    """Builder and persistence coordinator for WEMI cluster hierarchies."""

    def __init__(
        self,
        repository: WEMIRepository,
        identity_manager: Optional[ClusterIdentityManager] = None,
    ) -> None:
        self.repository = repository
        self.identity_manager = identity_manager or ClusterIdentityManager(repository)

    def create_work_cluster(
        self,
        canonical_record: MatchRecord,
        member_record_ids: list[str],
        existing_cluster_map: Optional[dict[str, str]] = None,
        hoisted_work_curie: Optional[str] = None,
    ) -> tuple[WorkCluster, list[Any], list[Any]]:
        """Resolve identity and create a WorkCluster entity."""
        (
            work_cluster_id,
            auth_status,
            access_point,
            events,
            redirections,
        ) = self.identity_manager.resolve_work_cluster_identity(
            canonical_title=canonical_record.uniform_title or canonical_record.title,
            canonical_author=canonical_record.author_key,
            work_type=canonical_record.work_type,
            member_record_ids=member_record_ids,
            existing_cluster_map=existing_cluster_map or {},
        )

        canonical_work_curie = hoisted_work_curie or self.identity_manager.format_canonical_uri(
            "work", work_cluster_id
        )

        cluster = WorkCluster(
            cluster_id=work_cluster_id,
            work_type=canonical_record.work_type,
            canonical_work_curie=canonical_work_curie,
            canonical_title=canonical_record.uniform_title or canonical_record.title,
            canonical_author=canonical_record.author_key,
            authority_status=auth_status,
            canonical_access_point=access_point,
        )
        return cluster, events, redirections

    def build_work_cluster_from_records(
        self,
        records: Sequence[MatchRecord],
        canonical_record: Optional[MatchRecord] = None,
    ) -> WorkCluster:
        """Construct a WorkCluster entity from a sequence of MatchRecord members."""
        if not records and canonical_record is None:
            raise ValueError("records or canonical_record must be provided")

        canon_rec = (
            canonical_record
            if canonical_record is not None
            else select_canonical_work_record(records)
        )
        seed = f"work::{canon_rec.author_key or ''}::{canon_rec.title_stem or canon_rec.title}"
        work_cluster_id = self.identity_manager.mint_pid("work", seed=seed)
        canonical_curie = self.identity_manager.format_canonical_uri("work", work_cluster_id)
        access_point = self.identity_manager.format_work_access_point(
            title=canon_rec.title,
            author=canon_rec.author_key,
            work_type=canon_rec.work_type,
        )

        member_list = records if records else [canon_rec]
        member_origins = [
            getattr(r, "source", None)
            or getattr(r, "record_origin", None)
            or (r.work_curie.split(":")[0] if r.work_curie and ":" in r.work_curie else "")
            for r in member_list
        ]
        authority_status = self.identity_manager.determine_authority_status(
            [o for o in member_origins if o]
        )

        return WorkCluster(
            cluster_id=work_cluster_id,
            work_type=canon_rec.work_type,
            canonical_work_curie=canonical_curie,
            canonical_title=canon_rec.title,
            canonical_author=canon_rec.author_key or None,
            authority_status=authority_status,
            canonical_access_point=access_point,
        )

    @staticmethod
    def mint_constituent_cluster_id(title: str, creator: Optional[str] = None) -> str:
        """Mint a deterministic cluster ID for an analytical constituent work."""
        auth_key = compute_author_key(creator)
        stem = compute_title_stem(title)
        h = hashlib.sha256(f"{auth_key}::{stem}".encode("utf-8")).hexdigest()[:16]
        return f"va_const_work_{h}"

    def build_constituent_work_cluster(
        self,
        title: str,
        creator: Optional[str] = None,
        work_type: WorkType = "text",
    ) -> WorkCluster:
        """Construct a WorkCluster entity for an analytical constituent."""
        cid = self.mint_constituent_cluster_id(title=title, creator=creator)
        access_point = f"{creator}: {title}" if creator else title
        return WorkCluster(
            cluster_id=cid,
            work_type=work_type,
            canonical_work_curie=f"reconciliator:{cid}",
            canonical_title=title,
            canonical_author=creator or None,
            authority_status="provisional",
            canonical_access_point=access_point,
        )

    def create_expression_cluster(
        self,
        work_cluster_id: str,
        content_type: str,
        language: str,
        version_designation: Optional[str] = None,
        hoisted_exp_curie: Optional[str] = None,
        seed: Optional[str] = None,
    ) -> ExpressionCluster:
        """Create an ExpressionCluster entity with deterministic or minted PID."""
        if hoisted_exp_curie:
            canonical_exp_curie = hoisted_exp_curie
            h_exp = hashlib.sha256(canonical_exp_curie.encode("utf-8")).hexdigest()[:16]
            exp_cluster_id = f"ec_{h_exp}"
        elif seed is not None:
            exp_cluster_id = self.identity_manager.mint_pid("expression", seed=seed)
            canonical_exp_curie = self.identity_manager.format_canonical_uri(
                "expression", exp_cluster_id
            )
        else:
            exp_sig = f"{work_cluster_id}::{content_type}::{language}::{version_designation or ''}"
            h_exp = hashlib.sha256(exp_sig.encode("utf-8")).hexdigest()[:16]
            exp_cluster_id = f"ec_{h_exp}"
            canonical_exp_curie = f"reconciliator:exp_{h_exp}"

        return ExpressionCluster(
            cluster_id=exp_cluster_id,
            work_cluster_id=work_cluster_id,
            canonical_expression_curie=canonical_exp_curie,
            content_type=content_type,  # type: ignore[arg-type]
            language=language,
            version_designation=version_designation,
        )

    def create_manifestation_cluster(
        self,
        canonical_record_id: str,
        publication_year: Optional[int] = None,
        seed: Optional[str] = None,
        publisher: Optional[str] = None,
    ) -> ManifestationCluster:
        """Create a ManifestationCluster entity."""
        if seed is not None:
            manif_cluster_id = self.identity_manager.mint_pid("manifestation", seed=seed)
        else:
            h_manif = hashlib.sha256(canonical_record_id.encode("utf-8")).hexdigest()[:16]
            manif_cluster_id = f"mc_{h_manif}"

        return ManifestationCluster(
            cluster_id=manif_cluster_id,
            canonical_record_id=canonical_record_id,
            publisher=publisher,
            publication_year=publication_year,
            member_record_count=1,
        )

    def persist_batch(
        self,
        work_clusters: Sequence[WorkCluster],
        expression_clusters: Sequence[ExpressionCluster],
        manifestation_clusters: Sequence[ManifestationCluster],
        corpus_linkages: Sequence[CorpusLinkage],
    ) -> None:
        """Persist all clustered WEMI entities and linkages to repository in batch."""
        if work_clusters:
            self.repository.insert_work_clusters_batch(list(work_clusters))
        if expression_clusters:
            self.repository.insert_expression_clusters_batch(list(expression_clusters))
        if manifestation_clusters:
            self.repository.insert_manifestation_clusters_batch(list(manifestation_clusters))
        if corpus_linkages:
            self.repository.insert_corpus_linkages_batch(list(corpus_linkages))
        logger.debug(
            "Persisted WEMI batch: %d works, %d expressions, %d manifestations, %d linkages",
            len(work_clusters),
            len(expression_clusters),
            len(manifestation_clusters),
            len(corpus_linkages),
        )
