"""Bridge vocabulary discovery and parsing.

Bridges foreign metadata vocabularies (e.g. LoC country codes, LoC language codes,
mode of issuance) to canonical YSO / Finto ontologies via skos:exactMatch.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import or_, select

from reconciliator.db import concept_mappings_table

_BRIDGES_DIR = Path(__file__).resolve().parent


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

    # Split on triple block boundaries (subject starting at line start followed by properties)
    # e.g.: loc:fi skos:exactMatch yso:p94426 ;
    # or <http://...> skos:exactMatch <http://...> ;
    blocks = re.split(r"(?:^|\n)(<[^>]+>|[a-zA-Z0-9_-]+:[a-zA-Z0-9_.-]+)\s+", content)
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

            labels: dict[str, str] = {
                lang.lower(): val for val, lang in re.findall(r'"([^"]+)"@([a-zA-Z_-]+)', body)
            }
            m_not = re.search(r'skos:notation\s+"([^"]+)"', body)

            bridges.append(
                {
                    "source_uri": subj_uri,
                    "target_uri": target_uri,
                    "bridge_vocab": bridge_vocab,
                    "notation": m_not.group(1) if m_not else None,
                    "pref_label_fi": labels.get("fi"),
                    "pref_label_sv": labels.get("sv"),
                    "pref_label_en": labels.get("en"),
                }
            )

    return bridges


_CACHED_MTS_ROLE_MAP: Optional[dict[str, str]] = None


def get_mts_role_map(vocab_store: Optional[Any] = None) -> dict[str, str]:
    """Dynamically construct MTS id -> MARC code map from vocab_store or loc-relators-bridge.ttl."""
    global _CACHED_MTS_ROLE_MAP

    # 1. If vocab_store is provided, attempt to look up exactMatch links
    if vocab_store is not None and hasattr(vocab_store, "get_connection"):
        try:
            with vocab_store.get_connection() as conn:
                stmt = select(
                    concept_mappings_table.c.legacy_vocab,
                    concept_mappings_table.c.legacy_id,
                    concept_mappings_table.c.target_vocab,
                    concept_mappings_table.c.target_id,
                ).where(
                    or_(
                        concept_mappings_table.c.legacy_vocab.in_(("loc-relators", "mts")),
                        concept_mappings_table.c.target_vocab.in_(("loc-relators", "mts")),
                    ),
                    concept_mappings_table.c.mapping_type == "exact_match",
                )
                rows = conn.execute(stmt).fetchall()
                if rows:
                    res: dict[str, str] = {}
                    for row in rows:
                        l_voc = str(row["legacy_vocab"])
                        l_id = str(row["legacy_id"]).lower()
                        t_voc = str(row["target_vocab"])
                        t_id = str(row["target_id"]).lower()
                        if l_voc == "loc-relators" and t_voc == "mts":
                            res[t_id] = l_id
                        elif t_voc == "loc-relators" and l_voc == "mts":
                            res[l_id] = t_id
                    if res:
                        return res
        except Exception:
            pass

    if _CACHED_MTS_ROLE_MAP is not None:
        return _CACHED_MTS_ROLE_MAP

    # 2. Parse from bundled loc-relators-bridge.ttl
    bridge_path = get_bundled_bridge_dir() / "loc-relators-bridge.ttl"
    if bridge_path.is_file():
        mappings = parse_bridge_turtle(bridge_path, bridge_vocab="loc-relators")
        res = {}
        for b in mappings:
            src = b["source_uri"]
            tgt = b["target_uri"]
            notation = b.get("notation")
            code = notation or (
                src.rstrip("/").split("/")[-1].lower() if "relators/" in src else None
            )
            if not code:
                continue
            token: Optional[str] = None
            if "urn:nbn:fi:au:mts:" in tgt.lower() or "finto.fi/mts" in tgt.lower():
                token = tgt.rstrip("/").split(":")[-1].split("/")[-1].lower()
            elif "urn:nbn:fi:au:mts:" in src.lower() or "finto.fi/mts" in src.lower():
                token = src.rstrip("/").split(":")[-1].split("/")[-1].lower()
            if token:
                res[token] = code
        if res:
            _CACHED_MTS_ROLE_MAP = res
            return _CACHED_MTS_ROLE_MAP

    # 3. Static fallback
    _CACHED_MTS_ROLE_MAP = {
        "m16": "aut",
        "m18": "trl",
        "m20": "ill",
        "m22": "cmp",
        "m24": "arr",
        "m26": "edt",
        "m28": "lyr",
        "m30": "pht",
        "m31": "cnd",
        "m32": "art",
        "m71": "drt",
        "m155": "prf",
        "m156": "act",
        "m157": "sng",
        "m158": "mus",
        "m163": "pro",
        "m167": "lbt",
        "m493": "pbl",
        "m494": "prt",
        "m495": "dst",
    }
    return _CACHED_MTS_ROLE_MAP


__all__ = [
    "get_bundled_bridge_dir",
    "get_bundled_bridge_files",
    "get_mts_role_map",
    "parse_bridge_turtle",
]
