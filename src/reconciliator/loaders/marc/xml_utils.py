"""MARCXML element and fast record navigation utilities."""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from lxml import etree

from reconciliator._core.parsers import MarcDatafield, MarcRecordData

MARC_NS = {"marc": "http://www.loc.gov/MARC21/slim"}


def derive_agent_id(
    name: Optional[str],
    dates: Optional[str] = None,
    fuller_name: Optional[str] = None,
) -> str:
    """Derive a stable agent id from the agent's own content."""
    parts = [
        (name or "").strip().lower(),
        (dates or "").strip().lower(),
        (fuller_name or "").strip().lower(),
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return digest


def derive_content_id(record_elem: Any, prefix: str = "sha1") -> str:
    """Derive a stable identifier from a record's own content."""
    if isinstance(record_elem, MarcRecordData):
        parts: list[str] = []
        if record_elem.leader:
            parts.append(f"L|{record_elem.leader.strip()}")
        for tag, val in record_elem.controlfields:
            parts.append(f"C|{tag}|{val.strip()}")
        for df in record_elem.datafields:
            subs = "|".join(f"{sf.code}={sf.value.strip()}" for sf in df.subfields if sf.code)
            parts.append(f"D|{df.tag}|{df.ind1 or ' '}{df.ind2 or ' '}|{subs}")
        digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
        return f"{prefix}:{digest}"

    parts = []
    for child in record_elem:
        tag = etree.QName(child).localname if isinstance(child.tag, str) else str(child.tag)
        if tag == "controlfield":
            parts.append(f"C|{child.get('tag')}|{(child.text or '').strip()}")
        elif tag == "datafield":
            subs = "|".join(
                f"{s.get('code')}={(s.text or '').strip()}" for s in child if isinstance(s.tag, str)
            )
            parts.append(
                f"D|{child.get('tag')}|{child.get('ind1') or ' '}{child.get('ind2') or ' '}|{subs}"
            )
        elif tag == "leader":
            parts.append(f"L|{(child.text or '').strip()}")
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _get_leader(record: Any) -> str:
    """Get text value of record leader."""
    if isinstance(record, MarcRecordData):
        return record.leader or ""
    leader_elem = record.find("marc:leader", MARC_NS)
    if leader_elem is None:
        leader_elem = record.find(".//leader")
    return (leader_elem.text or "") if leader_elem is not None else ""


def _get_subfields(elem: Any, code: str) -> list[str]:
    """Get text values of all subfields matching given code."""
    if isinstance(elem, MarcDatafield):
        return elem.get_subfields(code)
    res: list[str] = []
    for sub in elem.findall(f"marc:subfield[@code='{code}']", MARC_NS):
        if sub.text:
            res.append(sub.text.strip())
    # Also support element without namespace prefix if present
    if not res:
        for sub in elem.findall(f"subfield[@code='{code}']"):
            if sub.text:
                res.append(sub.text.strip())
    return res


def _get_subfield(elem: Any, code: str) -> Optional[str]:
    """Get text value of first subfield matching code."""
    if isinstance(elem, MarcDatafield):
        return elem.get_subfield(code)
    subs = _get_subfields(elem, code)
    return subs[0] if subs else None


def _get_datafields(record: Any, tag: str) -> list[Any]:
    """Get all datafield elements for a specific tag."""
    if isinstance(record, MarcRecordData):
        return record.get_datafields(tag)
    fields = record.findall(f"marc:datafield[@tag='{tag}']", MARC_NS)
    if not fields:
        fields = record.findall(f"datafield[@tag='{tag}']")
    return list(fields)


def _get_controlfield(record: Any, tag: str) -> Optional[str]:
    """Get value of controlfield element."""
    if isinstance(record, MarcRecordData):
        return record.get_controlfield(tag)
    cf = record.find(f"marc:controlfield[@tag='{tag}']", MARC_NS)
    if cf is None:
        cf = record.find(f"controlfield[@tag='{tag}']")
    return cf.text.strip() if cf is not None and cf.text else None
