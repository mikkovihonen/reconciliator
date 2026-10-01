"""BIBFRAME work type and expression content type classification."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from rdflib import RDF, Graph, IdentifiedNode, URIRef

if TYPE_CHECKING:
    from reconciliator.loaders.bibframe.profiles import BIBFRAMESchemaProfile



# BIBFRAME Work subclasses and bf:content values, mapped to WorkType. Unlike the
# MARC side - where classification comes from leader/007/008 positions and Finnish
# genre headings - the BIBFRAME vocabulary names these types explicitly, so the
# mapping is a lookup rather than a heuristic.
#
# Without it every BibFrame record defaulted to "text" (or "hub"/"music" under two
# graph conditions), so a BibFrame cartographic resource, thesis or dataset reached
# the monograph funnel: only 5 of 42 funnels were reachable from a BibFrame record.
BIBFRAME_WORK_TYPES: dict[str, str] = {
    "Cartography": "cartography",
    "Dataset": "dataset",
    "MovingImage": "moving_image",
    "StillImage": "still_image",
    "NotatedMusic": "music",
    "NotatedMovement": "choreography",
    "Audio": "music",
    "Object": "realia",
    "Multimedia": "multimedia",
    "Manuscript": "manuscript",
    "Archival": "archive_fonds",
    "Serial": "serial",
    "Integrating": "integrating",
    "Print": "text",
    "Text": "text",
}

# bf:content / rdaco values, checked after the class, since a Work may carry a
# generic class with a specific content type.
BIBFRAME_CONTENT_WORK_TYPES: dict[str, str] = {
    "cartographic": "cartography",
    "performed": "music",
    "notated-music": "music",
    "notated music": "music",
    "computer-dataset": "dataset",
    "computer-program": "software",
    "two-dimensional-moving-image": "moving_image",
    "three-dimensional-moving-image": "moving_image",
    "still-image": "still_image",
    "three-dimensional-form": "realia",
    "spoken-word": "audio_nonmusic",
}


_BIBFRAME = "http://id.loc.gov/ontologies/bibframe/"
_BF_CLASSIFYING_PREDICATES = (
    URIRef(_BIBFRAME + "content"),
    URIRef(_BIBFRAME + "genreForm"),
)


# The expression content type implied by a BIBFRAME class. Needed because the
# funnel split between notated music and recorded sound keys on content_type -
# both are work_type "music" - so without this every BibFrame music record, score
# or recording, reached the recorded-sound funnel.
BIBFRAME_CONTENT_TYPES: dict[str, str] = {
    "NotatedMusic": "notated_music",
    "Audio": "audio_music",
    "NotatedMovement": "choreographic_movement",
    "Cartography": "cartographic_image",
    "StillImage": "still_image",
    "Dataset": "dataset",
    "MovingImage": "moving_image",
    "Object": "three_dimensional",
}

def determine_bibframe_content_type(
    g: Graph,
    *nodes: Optional[IdentifiedNode],
    default: Optional[str] = None,
) -> Optional[str]:
    """The content type a BIBFRAME class implies, or `default` when unrecognised."""
    for node in nodes:
        if node is None:
            continue
        for type_uri in g.objects(node, RDF.type):
            local = str(type_uri).rstrip("/").split("/")[-1].split("#")[-1]
            if local in BIBFRAME_CONTENT_TYPES:
                return BIBFRAME_CONTENT_TYPES[local]
    return default


def determine_bibframe_work_type(
    g: Graph,
    *nodes: Optional[IdentifiedNode],
    default: str = "text",
) -> str:
    """Classify a BIBFRAME work from its rdf:type and bf:content.

    Checks each given node in order - typically the Work then the Instance - and
    returns the first recognised classification. Local-name matching, because
    profiles differ on whether they emit `bf:` or the expanded LoC namespace.
    """
    for node in nodes:
        if node is None:
            continue
        for type_uri in g.objects(node, RDF.type):
            local = str(type_uri).rstrip("/").split("/")[-1].split("#")[-1]
            if local in BIBFRAME_WORK_TYPES:
                return BIBFRAME_WORK_TYPES[local]

    for node in nodes:
        if node is None:
            continue
        for pred in _BF_CLASSIFYING_PREDICATES:
            for obj in g.objects(node, pred):
                text = str(obj).rstrip("/").split("/")[-1].split("#")[-1].lower()
                for needle, work_type in BIBFRAME_CONTENT_WORK_TYPES.items():
                    if needle in text:
                        return work_type

    return default


def _is_series(g: Graph, node: IdentifiedNode, profile: BIBFRAMESchemaProfile) -> bool:
    types = {str(t) for t in g.objects(node, RDF.type)}
    return bool(types & profile.expanded_series_classes)


