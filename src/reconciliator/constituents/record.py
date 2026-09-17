"""One extracted constituent, and what every format's extractor must do to a list.

Extraction is format-specific and stays with the format, as with subjects and
relators. Everything after it — deduplication, the cap, numbering — is the same
whatever the record was written in, so it lives here and no extractor gets to
have its own opinion about it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Optional

from reconciliator.constituents.spec import ConstituentProfile, ConstituentShape
from reconciliator.models import ComponentType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Constituent:
    """Something a publication contains: a track, a story, an article.

    `ordinal` is the position in the container, which is what makes a tracklist a
    tracklist rather than a set of titles; it is assigned by `finalise` after
    deduplication, so a track listed twice in one record does not consume two
    numbers.
    """

    title: str
    creator: Optional[str] = None
    ordinal: Optional[int] = None
    disc: Optional[int] = None
    duration: Optional[str] = None
    pagination: Optional[str] = None
    role: Optional[str] = None

    shape: ConstituentShape = "embodied"
    component_type: Optional[ComponentType] = None
    container_key: Optional[str] = None

    #: The container's record control number, as the record states it.
    container_ref: Optional[str] = None

    #: An identity the source states for this constituent, used instead of a
    #: content digest. An RDF part is a node with a URI of its own, and the graph
    #: saying so is better evidence than anything derived from its title.
    identity_ref: Optional[str] = None

    #: Whether the agent named beside this constituent is also a contributor to
    #: the container. See ConstituentField.contributes_to_container.
    contributes_to_container: bool = False

    #: The MARC tag or RDF predicate this came from, for diagnostics and for the
    #: reachability audit.
    source_field: Optional[str] = None

    #: Index of the profile field entry this came from. Profile order is
    #: precedence, so this decides which description of one constituent wins.
    precedence: int = 0

    #: Whether `creator` is an authority-controlled name rather than a
    #: transcription. See ConstituentField.creator_controlled.
    creator_controlled: bool = False


def finalise(
    constituents: list[Constituent],
    profile: ConstituentProfile,
    *,
    record_id: str = "<unknown>",
) -> list[Constituent]:
    """Deduplicate, cap and number, per the profile.

    Order in is significant: it is the order the profile's fields were read in,
    and for `ordinal: position` it becomes the position in the container.
    """
    deduped = _dedup(constituents, profile)

    if len(deduped) > profile.max_per_record:
        # A real contents note can list a 500-track box set, and a malformed one
        # can list anything at all. Truncating loudly beats writing 500 works.
        logger.warning(
            "Record %s lists %d constituents; profile %r caps at %d. Keeping the first %d.",
            record_id,
            len(deduped),
            profile.name,
            profile.max_per_record,
            profile.max_per_record,
        )
        deduped = deduped[: profile.max_per_record]

    numbered: list[Constituent] = []
    position = 0
    for constituent in deduped:
        if constituent.ordinal is None and _numbers_by_position(profile, constituent):
            position += 1
            numbered.append(replace(constituent, ordinal=position))
        else:
            numbered.append(constituent)
            if constituent.ordinal is not None:
                position = max(position, constituent.ordinal)
    return numbered


def _numbers_by_position(profile: ConstituentProfile, constituent: Constituent) -> bool:
    """Whether this constituent's field entry numbers by document order."""
    if 0 <= constituent.precedence < len(profile.fields):
        return profile.fields[constituent.precedence].ordinal == "position"
    return False


def _dedup(constituents: list[Constituent], profile: ConstituentProfile) -> list[Constituent]:
    if profile.dedup == "none" or not constituents:
        return list(constituents)

    from reconciliator.normalizers.blocking import compute_title_stem

    kept: list[Constituent] = []
    index: dict[tuple[str, str], int] = {}
    for constituent in constituents:
        stem = compute_title_stem(constituent.title) or constituent.title.strip().casefold()
        creator = ""
        if profile.dedup == "title_and_creator" and constituent.creator:
            creator = constituent.creator.strip().casefold()
        key = (stem, creator)

        seen_at = index.get(key)
        if seen_at is None:
            index[key] = len(kept)
            kept.append(constituent)
            continue

        kept[seen_at] = _merge(kept[seen_at], constituent)
    return kept


def _merge(first: Constituent, second: Constituent) -> Constituent:
    """One constituent described twice in one record, reconciled attribute by
    attribute.

    Two entries for one track are two descriptions of it, not two candidates: an
    album's contents note gives the duration and its added entry gives the
    composer. So each attribute is taken from the higher-precedence entry that
    states one - precedence being the profile's field order, which is what makes
    it the source's decision rather than this function's.

    One exception, and it is the reason `creator_controlled` exists: an
    authority-controlled name beats a transcribed one whichever entry otherwise
    wins, because "Rautavaara, Einojuhani" and "Rautavaara" are the same person
    and only the first will match an authority record.
    """
    primary, secondary = (
        (first, second) if first.precedence <= second.precedence else (second, first)
    )

    creator, controlled = primary.creator, primary.creator_controlled
    if secondary.creator and secondary.creator_controlled and not primary.creator_controlled:
        creator, controlled = secondary.creator, True
    elif not creator:
        creator, controlled = secondary.creator, secondary.creator_controlled

    return replace(
        primary,
        creator=creator,
        creator_controlled=controlled,
        duration=primary.duration or secondary.duration,
        pagination=primary.pagination or secondary.pagination,
        disc=primary.disc if primary.disc is not None else secondary.disc,
        role=primary.role or secondary.role,
        ordinal=primary.ordinal if primary.ordinal is not None else secondary.ordinal,
        # A constituent that any entry says contributes to the container does.
        contributes_to_container=(
            primary.contributes_to_container or secondary.contributes_to_container
        ),
    )
