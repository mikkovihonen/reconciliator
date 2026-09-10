"""Bridge vocabulary discovery and parsing.

Bridges foreign metadata vocabularies (e.g. LoC country codes, LoC language codes,
mode of issuance) to canonical YSO / Finto ontologies via skos:exactMatch.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_BRIDGES_DIR = Path(__file__).resolve().parent / "bridges"


def get_bundled_bridge_dir() -> Path:
    """Return the absolute path to the bundled bridge vocabularies directory."""
    return _BRIDGES_DIR


def get_bundled_bridge_files() -> list[Path]:
    """Return list of existing bundled bridge TTL file paths."""
    if not _BRIDGES_DIR.is_dir():
        return []
    return sorted(_BRIDGES_DIR.glob("*.ttl"))


def parse_bridge_turtle(content: str | Path, bridge_vocab: str = "bridge") -> list[dict[str, Any]]:
    """Parse bridge Turtle content into structured bridge mappings.

    Extracts:
    - source_uri: e.g. <http://id.loc.gov/vocabulary/countries/fi>
    - target_uri: e.g. <http://www.yso.fi/onto/yso/p94426>
    - notation: e.g. "fi"
    - pref_label_fi, pref_label_sv, pref_label_en
    """
    if isinstance(content, Path):
        content = content.read_text(encoding="utf-8")

    prefixes: dict[str, str] = {
        "skos:": "http://www.w3.org/2004/02/skos/core#",
        "yso:": "http://www.yso.fi/onto/yso/",
        "loc:": "http://id.loc.gov/vocabulary/",
    }
    for m in re.finditer(
        r"(?:@prefix|PREFIX)\s+([a-zA-Z0-9_-]+:)\s+<([^>]+)>", content, re.IGNORECASE
    ):
        prefixes[m.group(1)] = m.group(2)

    def _resolve(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("<") and raw.endswith(">"):
            return raw[1:-1]
        for pfx, base in prefixes.items():
            if raw.startswith(pfx):
                return base + raw[len(pfx) :]
        return raw

    bridges: list[dict[str, Any]] = []

    # Split on triple block boundaries (subject followed by properties)
    # e.g.: loc:fi skos:exactMatch yso:p94426 ;
    # or <http://...> skos:exactMatch <http://...> ;
    blocks = re.split(r"(?:^|\n)\s*(<[^>]+>|[a-zA-Z0-9_-]+:[a-zA-Z0-9_.-]+)\s+", content)
    if len(blocks) > 1:
        for i in range(1, len(blocks), 2):
            raw_subj = blocks[i]
            body = blocks[i + 1]
            if "skos:exactMatch" not in body and "owl:sameAs" not in body:
                continue

            subj_uri = _resolve(raw_subj)

            m_match = re.search(
                r"(?:skos:exactMatch|owl:sameAs)\s+(<[^>]+>|[a-zA-Z0-9_-]+:[a-zA-Z0-9_.-]+)",
                body,
            )
            if not m_match:
                continue
            target_uri = _resolve(m_match.group(1))

            m_fi = re.search(r'skos:prefLabel\s+"([^"]+)"@fi', body)
            m_sv = re.search(r'skos:prefLabel\s+"([^"]+)"@sv', body)
            m_en = re.search(r'skos:prefLabel\s+"([^"]+)"@en', body)
            m_not = re.search(r'skos:notation\s+"([^"]+)"', body)

            bridges.append(
                {
                    "source_uri": subj_uri,
                    "target_uri": target_uri,
                    "bridge_vocab": bridge_vocab,
                    "notation": m_not.group(1) if m_not else None,
                    "pref_label_fi": m_fi.group(1) if m_fi else None,
                    "pref_label_sv": m_sv.group(1) if m_sv else None,
                    "pref_label_en": m_en.group(1) if m_en else None,
                }
            )

    return bridges


__all__ = [
    "get_bundled_bridge_dir",
    "get_bundled_bridge_files",
    "parse_bridge_turtle",
]
