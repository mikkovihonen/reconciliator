"""Turning extracted constituents into rows, once, for every format.

Extraction is the source's business and differs per format and per catalogue
(`constituents/`). What a constituent *becomes* in the datastore must not differ,
or two formats describing one album would store two different shapes of the same
fact — the mistake the loader contract was written to stop.

This module is that half: `Constituent` objects in, WEMI rows out.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional

from reconciliator.constituents.record import Constituent
from reconciliator.models import (
    ExpressionContentType,
    ExpressionEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    WorkEntity,
    WorkRelationship,
    WorkType,
)
from reconciliator.normalizers.blocking import compute_author_key, compute_title_stem

logger = logging.getLogger(__name__)


@dataclass
class ConstituentRows:
    """What one record's constituents add to the batch."""

    works: list[WorkEntity] = field(default_factory=list)
    expressions: list[ExpressionEntity] = field(default_factory=list)
    associations: list[ManifestationExpressionAssociation] = field(default_factory=list)
    relationships: list[WorkRelationship] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.works or self.expressions or self.associations)


def derive_constituent_id(
    title: str, creator: Optional[str], work_type: str, *, language: Optional[str] = None
) -> str:
    """A content-derived identity for a constituent work.

    `works` is `UNIQUE(work_uri)` and its upsert conflicts on that column, so two
    albums in one source that share a track converge on a single work row — which
    is the point: it is one work, and a tracklist that matched only within one
    release would be no use.

    The digest covers author key, title stem and work type. `work_type` narrows
    the collision space cheaply; the container is deliberately *not* in it,
    because including it would defeat the convergence this exists for.

    Cross-source identity stays consolidation's job, as everywhere else.
    """
    author = compute_author_key(creator, None) or ""
    stem = compute_title_stem(title, lang=language) or title.strip().casefold()
    digest = hashlib.sha1(f"{author}|{stem}|{work_type}".encode("utf-8")).hexdigest()
    return f"c:{digest[:16]}"


def build_embodied_constituents(
    constituents: list[Constituent],
    *,
    container_work: WorkEntity,
    container_manifestation: ManifestationEntity,
    source: str,
    record_origin: str,
    language: str,
    work_type: WorkType,
    content_type: ExpressionContentType,
    container_creator: Optional[str] = None,
    serialization_engine: Optional[object] = None,
) -> ConstituentRows:
    """Rows for constituents embodied in the container's manifestation.

    An album track and an anthology story have no carrier of their own - no
    publisher, no extent - so they get no manifestation row. They are Works
    realised as Expressions *inside the container's manifestation*, which is
    exactly what `manifestation_expressions` records, and what its
    `track_number`, `disc_number` and `duration` columns are for.

    A constituent with its own extent is a different shape and is not handled
    here; see plans/aggregates.md, stage 6.
    """
    rows = ConstituentRows()
    if not container_manifestation.manifestation_uri:
        # Without it there is nothing for the junction row to name, and binding
        # positionally is what collapsed a tracklist into one row before.
        logger.warning(
            "Source %s record %s has no manifestation URI; skipping %d constituents.",
            source,
            container_work.source_id,
            len(constituents),
        )
        return rows

    embodied = [c for c in constituents if c.shape == "embodied"]

    # What the container's own identity would be, had it been read as a
    # constituent - and whether the container is really an aggregate at all. A
    # release whose note lists exactly one work, itself, *is* that work, and the
    # container row already represents it. A disc named after one of the several
    # works on it is a different case: "Cantus arcticus" is both the album title
    # and one of its two pieces, and that piece still belongs in the tracklist.
    container_identity = derive_constituent_id(
        container_work.title, container_creator, work_type, language=language
    )
    is_single_work_release = len(embodied) == 1

    seen: set[str] = set()
    for constituent in embodied:
        # A constituent that names no creator inherits the container's main
        # entry, which is the cataloguing rule and the difference between
        # convergence and two work rows: one release names a track's composer in
        # its contents note and the next does not, and without this they are two
        # works. `container_creator` is None for a record with no 1XX - a
        # various-artists compilation - which is when inheriting would be wrong.
        source_id = derive_constituent_id(
            constituent.title,
            constituent.creator or container_creator,
            work_type,
            language=language,
        )
        if is_single_work_release and source_id == container_identity:
            # Comparing URIs here instead - as the first attempt did - compares a
            # control-number URI with a digest URI, which can never be equal, so
            # the guard never fired at all and a single-work release got a second
            # work row for its own work plus an edge to it.
            continue

        work_uri = f"urn:work:{source}:{source_id}"
        if source_id in seen:
            # Two constituents that differ in title but agree once stemmed. They
            # are one work row, and a second junction row for the same
            # expression would collide on the junction's primary key.
            continue
        seen.add(source_id)

        expression_uri = f"urn:exp:{source}:{source_id}:{language}"
        rows.works.append(
            WorkEntity(
                work_uri=work_uri,
                source=source,
                source_id=source_id,
                record_origin=record_origin,
                title=constituent.title,
                title_normalized=compute_title_stem(constituent.title, lang=language)
                or constituent.title.strip().lower(),
                original_language=language,
                work_type=work_type,
            )
        )
        rows.expressions.append(
            ExpressionEntity(
                work_id=0,
                expression_uri=expression_uri,
                # The same (source, source_id) as its work, which is how
                # insert_wemi_batch binds an expression to the right work when a
                # record yields more than one.
                source=source,
                source_id=source_id,
                record_origin=record_origin,
                language=language,
                content_type=content_type,
                title=constituent.title,
            )
        )
        rows.associations.append(
            ManifestationExpressionAssociation(
                manifestation_id=0,
                expression_id=0,
                manifestation_uri=container_manifestation.manifestation_uri,
                expression_uri=expression_uri,
                track_number=constituent.ordinal,
                disc_number=constituent.disc or 1,
                duration=constituent.duration,
                # The container's own expression is the primary one; a track is
                # not a second candidate for "what this manifestation is".
                is_primary=False,
            )
        )
        rows.relationships.append(
            WorkRelationship(
                source_work_id=0,
                target_work_id=0,
                source_work_uri=container_work.work_uri,
                target_work_uri=work_uri,
                relationship_type="has_constituent",
            )
        )

    if rows.works:
        _serialize(rows, serialization_engine)
    return rows


def _serialize(rows: ConstituentRows, engine: Optional[object]) -> None:
    """Pre-render each constituent work, as the container's tiers are.

    `serialized_text` is what BM25 and the dense index read, so a constituent
    with none is a work no vector search can reach.
    """
    if engine is None or not hasattr(engine, "render_work"):
        return
    for work in rows.works:
        try:
            work.serialized_text = engine.render_work(work, authors=[])
        except Exception:  # a template failure must not lose the row
            logger.debug("Could not serialise constituent %s", work.work_uri, exc_info=True)
