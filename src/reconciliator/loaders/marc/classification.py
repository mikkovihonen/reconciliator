"""Classification of MARC records into WEMI WorkType, ExpressionContentType, and CarrierType."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional

from lxml import etree

from reconciliator.config import (
    get_carrier_type_mappings,
    get_category_default_field_map,
    get_content_type_work_types,
    get_genre_patterns,
    get_genre_work_types,
    get_rda_content_types,
    get_tactile_content_codes,
)
from reconciliator.loaders.marc.xml_utils import (
    _get_controlfield,
    _get_datafields,
    _get_leader,
    _get_subfield,
    _get_subfields,
)
from reconciliator.models import CarrierType, ExpressionContentType, WorkType

GENRE_WORK_TYPES: dict[str, dict[str, str]] = get_genre_work_types()
GENRE_PATTERNS: dict[str, list[str]] = get_genre_patterns()
RDA_CONTENT_TYPES: dict[str, str] = get_rda_content_types()
CONTENT_TYPE_WORK_TYPES: dict[str, str] = get_content_type_work_types()
TACTILE_CONTENT_CODES: set[str] = get_tactile_content_codes()
CARRIER_TYPE_MAPPINGS: dict[str, str] = get_carrier_type_mappings()


def determine_work_type(
    record_elem: etree._Element,
    genre_work_types: Optional[dict[str, dict[str, str]]] = None,
    genre_patterns: Optional[dict[str, list[str]]] = None,
    content_type_work_types: Optional[dict[str, str]] = None,
    tactile_content_codes: Optional[Iterable[str]] = None,
    config: Optional[Any] = None,
) -> WorkType:
    """Determine WEMI WorkType from Leader/06-07, 007, 008, 336, 502, and 655."""
    gw_types = genre_work_types if genre_work_types is not None else get_genre_work_types(config)
    gp_patterns = genre_patterns if genre_patterns is not None else get_genre_patterns(config)
    ct_work_types = (
        content_type_work_types
        if content_type_work_types is not None
        else get_content_type_work_types(config)
    )
    tactile_codes = (
        set(tactile_content_codes)
        if tactile_content_codes is not None
        else get_tactile_content_codes(config)
    )

    ldr = _get_leader(record_elem)
    l06 = ldr[6:7] if len(ldr) > 6 else ""
    l07 = ldr[7:8] if len(ldr) > 7 else ""

    f008 = _get_controlfield(record_elem, "008") or ""

    # Check 502 (Academic Thesis)
    if _get_datafields(record_elem, "502"):
        return "thesis"

    # Check genre/form headings
    genre_field = get_category_default_field_map()["genre_form"]
    genre_elements = _get_datafields(record_elem, genre_field)

    # 1. Exact match against configured genre vocabularies
    for f in genre_elements:
        gh = (_get_subfield(f, "a") or "").lower().strip()
        if not gh:
            continue
        vocab = (_get_subfield(f, "2") or "").lower().strip()
        vocab_base = vocab.split("/")[0] if vocab else ""

        if vocab_base and vocab_base in gw_types:
            resolved = gw_types[vocab_base].get(gh)
            if resolved:
                return resolved  # type: ignore[return-value]

        for v_dict in gw_types.values():
            resolved = v_dict.get(gh)
            if resolved:
                return resolved  # type: ignore[return-value]

    # 2. Substring pattern heuristics from configuration
    for f in genre_elements:
        gh = (_get_subfield(f, "a") or "").lower()
        for target_type, patterns in gp_patterns.items():
            if any(p in gh for p in patterns):
                return target_type  # type: ignore[return-value]

    # Tactile resources
    for f in _get_datafields(record_elem, "341"):
        for val in _get_subfields(f, "a"):
            if val.strip().lower() == "tactile":
                return "tactile"
    for f in _get_datafields(record_elem, "336"):
        for sub in _get_subfields(f, "b"):
            if sub.strip().lower() in tactile_codes:
                return "tactile"

    # A 111 meeting main entry is a conference.
    if _get_datafields(record_elem, "111"):
        return "conference"

    # Music
    if l06 in ("c", "d", "j"):
        return "music"
    if l06 == "i":
        return "audio_nonmusic"

    # Cartography
    if l06 in ("e", "f"):
        return "cartography"

    # Visual / Moving Image
    if l06 in ("g", "v"):
        return "moving_image"
    if l06 == "k":
        return "still_image"

    # Computer files / Software / Datasets
    if l06 == "m":
        if len(f008) > 26 and f008[26] in ("a", "b", "c", "h"):
            return "dataset"
        return "software"

    # Manuscripts
    if l06 == "t":
        return "manuscript"
    if l06 == "p":
        return "archive_fonds"
    if l06 == "r":
        return "realia"
    if l06 == "o":
        return "multimedia"

    # Early printed books (Date1 < 1800)
    date1 = f008[7:11] if len(f008) >= 11 and f008[7:11].isdigit() else None
    if date1 and int(date1) < 1800:
        return "early_print"

    # Web archive and digital surrogate
    for f in _get_datafields(record_elem, "857"):
        relationship = (f.get("ind2") or " ").strip()
        if relationship in ("1", "4"):
            return "preservation_surrogate"
        return "web_harvest"
    if _get_datafields(record_elem, "533"):
        return "preservation_surrogate"

    # RDA content type as a work-type signal
    for f in _get_datafields(record_elem, "336"):
        for sub in _get_subfields(f, "b"):
            implied = ct_work_types.get(sub.strip().lower())
            if implied:
                return implied  # type: ignore[return-value]

    # Serials / Integrating
    if l07 in ("s", "b"):
        return "serial"
    if l07 == "i" or (len(f008) > 21 and f008[21] in ("d", "w")):
        return "integrating"

    return "text"


def determine_content_type(
    record_elem: etree._Element,
    work_type: WorkType = "text",
    rda_content_types: Optional[dict[str, str]] = None,
    config: Optional[Any] = None,
) -> ExpressionContentType:
    """Determine WEMI ExpressionContentType from 336 $b, Leader/06, or work_type."""
    rc_types = rda_content_types if rda_content_types is not None else get_rda_content_types(config)
    for f in _get_datafields(record_elem, "336"):
        for sub in _get_subfields(f, "b"):
            resolved = rc_types.get(sub.lower().strip())
            if resolved:
                return resolved  # type: ignore[return-value]

    if work_type == "music":
        ldr = _get_leader(record_elem)
        l06 = ldr[6:7] if len(ldr) > 6 else ""
        return "audio_music" if l06 == "j" else "notated_music"
    if work_type == "audio_nonmusic":
        return "spoken_word"
    if work_type == "moving_image":
        return "moving_image"
    if work_type == "cartography":
        return "cartographic_image"
    if work_type == "still_image":
        return "still_image"
    if work_type in ("software", "ai_model"):
        return "software_code"
    if work_type == "dataset":
        return "dataset"
    if work_type == "tactile":
        return "tactile_text"

    return "text"


def determine_carrier_type(
    record_elem: etree._Element,
    carrier_type_mappings: Optional[dict[str, str]] = None,
    config: Optional[Any] = None,
) -> CarrierType:
    """Determine WEMI CarrierType from 338 $b or Leader/07/007."""
    c_mappings = (
        carrier_type_mappings
        if carrier_type_mappings is not None
        else get_carrier_type_mappings(config)
    )
    for f in _get_datafields(record_elem, "338"):
        for sub in _get_subfields(f, "b"):
            code = sub.lower().strip()
            if code in c_mappings:
                return c_mappings[code]  # type: ignore[return-value]

    f007 = _get_controlfield(record_elem, "007") or ""
    if f007:
        c0 = f007[0].lower()
        if c0 == "c":
            return (
                "online_resource" if len(f007) > 1 and f007[1].lower() == "r" else "computer_disc"
            )
        if c0 == "s":
            return "audio_disc" if len(f007) > 1 and f007[1].lower() == "d" else "audiocassette"
        if c0 == "v":
            return "videodisc" if len(f007) > 1 and f007[1].lower() == "d" else "videocassette"
        if c0 == "h":
            return "microfilm_reel"
        if c0 == "f":
            return "braille_volume"

    return "volume"
