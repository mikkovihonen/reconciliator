"""Constituent parts and tracks extraction from MARC bibliographic records."""

from __future__ import annotations

import re
from typing import Optional

from lxml import etree
from reconciliator._core.parsers import MarcDatafield

from reconciliator.constituents import ConstituentProfile
from reconciliator.constituents.record import Constituent
from reconciliator.constituents.record import finalise as finalise_constituents
from reconciliator.loaders.base import strip_isbd
from reconciliator.loaders.marc.xml_utils import _get_datafields


def _subfield_groups(
    datafield: etree._Element, title_code: str
) -> list[tuple[dict[str, str], dict[str, str]]]:
    """Split a datafield into one group per title subfield, in document order.

    An enhanced 505 repeats its subfields in groups - `$t` title `$r`
    responsibility `$g` duration, again and again - so a constituent's attributes
    are the ones that follow *its* `$t` and precede the next.
    """
    ordered: list[tuple[str, str]] = []
    if isinstance(datafield, MarcDatafield):
        ordered = [
            (sf.code, sf.value.strip())
            for sf in datafield.subfields
            if sf.code and sf.value.strip()
        ]
    else:
        for sub in datafield:
            tag = etree.QName(sub).localname if isinstance(sub.tag, str) else ""
            if tag != "subfield":
                continue
            code = sub.get("code") or ""
            text = (sub.text or "").strip()
            if code and text:
                ordered.append((code, text))

    preamble: dict[str, str] = {}
    for code, text in ordered:
        if code == title_code:
            break
        preamble.setdefault(code, text)

    groups: list[tuple[dict[str, str], dict[str, str]]] = []
    current: Optional[dict[str, str]] = None
    for code, text in ordered:
        if code == title_code:
            current = {code: text}
            groups.append((current, preamble))
        elif current is not None:
            current.setdefault(code, text)
    return groups


def _indicators_match(datafield: etree._Element, required: dict[str, str]) -> bool:
    """Whether a datafield's indicators satisfy the profile."""
    for name, expected in required.items():
        actual = datafield.get(name)
        if (actual or " ").strip() != (expected or " ").strip():
            return False
    return True


def _stated_ordinal(
    ordinal: str, group: dict[str, str], field_level: dict[str, str]
) -> Optional[int]:
    """A number the record states, for `ordinal: subfield:<code>`."""
    if not ordinal.startswith("subfield:"):
        return None
    code = ordinal.split(":", 1)[1]
    return _as_int(group.get(code) or field_level.get(code))


def _as_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def _clean_duration(value: Optional[str]) -> Optional[str]:
    """Clean "(4:32)" to "4:32"."""
    if not value:
        return None
    cleaned = value.strip().strip("()[]").strip()
    return cleaned or None


def extract_marc_constituents(
    record_elem: etree._Element,
    profile: ConstituentProfile,
    *,
    record_id: str = "<unknown>",
) -> list[Constituent]:
    """Read the constituents a profile says this source records."""
    found: list[Constituent] = []

    for precedence, fld_spec in enumerate(profile.fields):
        if not fld_spec.tag:
            continue
        for datafield in _get_datafields(record_elem, fld_spec.tag):
            if not _indicators_match(datafield, fld_spec.indicators):
                continue
            for group, field_level in _subfield_groups(datafield, fld_spec.title or "t"):

                def value(code: Optional[str]) -> Optional[str]:
                    if not code:
                        return None
                    return group.get(code) or field_level.get(code)

                raw_title = value(fld_spec.title)
                title = strip_isbd(raw_title).strip() if raw_title else None
                if not title:
                    continue
                raw_creator = value(fld_spec.creator)
                creator = strip_isbd(raw_creator).strip() if raw_creator else None
                found.append(
                    Constituent(
                        title=title,
                        creator=creator,
                        ordinal=_stated_ordinal(fld_spec.ordinal, group, field_level),
                        disc=_as_int(value(fld_spec.disc)),
                        duration=_clean_duration(value(fld_spec.duration)),
                        pagination=value(fld_spec.pagination),
                        role=value(fld_spec.role),
                        shape=profile.shape_of(fld_spec),
                        component_type=fld_spec.component_type,
                        container_key=fld_spec.container_key,
                        container_ref=value(fld_spec.container_source_id),
                        contributes_to_container=fld_spec.contributes_to_container,
                        source_field=fld_spec.tag,
                        precedence=precedence,
                        creator_controlled=fld_spec.creator_controlled,
                    )
                )

    return finalise_constituents(found, profile, record_id=record_id)
