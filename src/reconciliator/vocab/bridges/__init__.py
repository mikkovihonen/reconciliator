"""Bridge vocabulary discovery and parsing.

Bridges foreign metadata vocabularies (e.g. LoC country codes, LoC language codes,
LoC relator codes, mode of issuance) to canonical ontologies via skos:exactMatch.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select

from reconciliator.database import concept_mappings_table

_BRIDGES_DIR = Path(__file__).resolve().parent


def get_bundled_bridge_dir() -> Path:
    """Return the absolute path to the bundled bridge vocabularies directory."""
    return _BRIDGES_DIR


def get_bundled_bridge_files() -> list[Path]:
    """Return list of existing bundled bridge TTL file paths."""
    if not _BRIDGES_DIR.is_dir():
        return []
    return sorted(_BRIDGES_DIR.glob("*.ttl"))


def resolve_bridge_files(config: Optional[Any] = None) -> list[Path]:
    """Resolve bridge vocabulary files based on configuration or bundled defaults."""
    if config is not None and getattr(config, "enable_bridges", True) is False:
        return []

    # 1. Explicit list of bridge files
    custom_files = getattr(config, "bridge_files", None) if config else None
    if custom_files:
        return [Path(p) for p in custom_files if Path(p).is_file()]

    # 2. Custom bridge directory
    custom_dir = getattr(config, "bridge_dir", None) if config else None
    if custom_dir and Path(custom_dir).is_dir():
        files = sorted(Path(custom_dir).glob("*.ttl"))
        if files:
            return files

    # 3. Bundled files with optional relators override
    files = list(get_bundled_bridge_files())
    relators_override = getattr(config, "relators_bridge_file", None) if config else None
    if relators_override and Path(relators_override).is_file():
        files = [f for f in files if "relator" not in f.name.lower()]
        files.append(Path(relators_override))

    return files


def parse_bridge_turtle(content: str | Path, bridge_vocab: str = "bridge") -> list[dict[str, Any]]:
    """Parse bridge Turtle content into structured bridge mappings.

    Extracts:
    - source_uri: e.g. <http://id.loc.gov/vocabulary/countries/fi>
    - target_uri: e.g. <http://www.yso.fi/onto/yso/p94426>
    - notation: e.g. "fi"
    - pref_labels: dict[lang, label]
    - alt_labels: dict[lang, list[label]]
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

            pref_labels: dict[str, str] = {}
            alt_labels: dict[str, list[str]] = {}

            # Parse skos:prefLabel clauses
            for pref_m in re.finditer(
                r"skos:prefLabel\s+((?:\"[^\"]*\"@[a-zA-Z_-]+\s*,\s*)*\"[^\"]*\"@[a-zA-Z_-]+)",
                body,
            ):
                for val, lang in re.findall(r'"([^"]+)"@([a-zA-Z_-]+)', pref_m.group(1)):
                    pref_labels[lang.lower()] = val

            # Parse rdfs:label if prefLabel not found
            if not pref_labels:
                for rdfs_m in re.finditer(
                    r"rdfs:label\s+((?:\"[^\"]*\"@[a-zA-Z_-]+\s*,\s*)*\"[^\"]*\"@[a-zA-Z_-]+)",
                    body,
                ):
                    for val, lang in re.findall(r'"([^"]+)"@([a-zA-Z_-]+)', rdfs_m.group(1)):
                        pref_labels[lang.lower()] = val

            # Parse skos:altLabel clauses
            for alt_m in re.finditer(
                r"skos:altLabel\s+((?:\"[^\"]*\"@[a-zA-Z_-]+\s*,\s*)*\"[^\"]*\"@[a-zA-Z_-]+)",
                body,
            ):
                for val, lang in re.findall(r'"([^"]+)"@([a-zA-Z_-]+)', alt_m.group(1)):
                    alt_labels.setdefault(lang.lower(), []).append(val)

            # Fallback if no specific clauses parsed
            if not pref_labels:
                for val, lang in re.findall(r'"([^"]+)"@([a-zA-Z_-]+)', body):
                    pref_labels.setdefault(lang.lower(), val)

            m_not = re.search(r'skos:notation\s+"([^"]+)"', body)

            item: dict[str, Any] = {
                "source_uri": subj_uri,
                "target_uri": target_uri,
                "bridge_vocab": bridge_vocab,
                "notation": m_not.group(1) if m_not else None,
                "pref_labels": pref_labels,
                "alt_labels": alt_labels,
            }
            for lang, val in pref_labels.items():
                item[f"pref_label_{lang}"] = val

            bridges.append(item)

    return bridges


_CACHED_BRIDGED_ROLE_MAP: Optional[dict[str, str]] = None


def get_bridged_role_map(
    vocab_store: Optional[Any] = None,
    bridge_file: Optional[Path | str] = None,
) -> dict[str, str]:
    """Dynamically construct bridged concept / URI / token -> MARC relator code map."""
    global _CACHED_BRIDGED_ROLE_MAP
    if bridge_file is None and vocab_store is None and _CACHED_BRIDGED_ROLE_MAP is not None:
        return _CACHED_BRIDGED_ROLE_MAP

    res: dict[str, str] = {}

    # 1. If vocab_store is provided, look up exactMatch links from concept_mappings
    if vocab_store is not None and hasattr(vocab_store, "engine"):
        try:
            with vocab_store.engine.connect() as conn:
                stmt = select(
                    concept_mappings_table.c.legacy_uri,
                    concept_mappings_table.c.legacy_id,
                    concept_mappings_table.c.target_uri,
                    concept_mappings_table.c.target_id,
                ).where(
                    concept_mappings_table.c.mapping_type == "exact_match",
                )
                rows = conn.execute(stmt).fetchall()
                for row in rows:
                    l_uri = str(row[0] or "")
                    l_id = str(row[1] or "").lower()
                    t_uri = str(row[2] or "")
                    t_id = str(row[3] or "").lower()

                    code = None
                    other_uri = None
                    other_id = None
                    if "relators/" in l_uri.lower():
                        code = l_uri.rstrip("/").split("/")[-1].lower()
                        other_uri = t_uri
                        other_id = t_id
                    elif "relators/" in t_uri.lower():
                        code = t_uri.rstrip("/").split("/")[-1].lower()
                        other_uri = l_uri
                        other_id = l_id

                    if code and len(code) == 3:
                        if other_uri:
                            res[other_uri.lower()] = code
                            token = (
                                other_uri.rstrip("/")
                                .split(":")[-1]
                                .split("/")[-1]
                                .split("#")[-1]
                                .lower()
                            )
                            res[token] = code
                        if other_id:
                            res[other_id] = code
                if res:
                    return res
        except Exception:
            pass

    # 2. Parse from specified bridge file or bundled loc-relators-bridge.ttl
    b_path = (
        Path(bridge_file)
        if bridge_file
        else get_bundled_bridge_dir() / "loc-relators-bridge.ttl"
    )
    if b_path.is_file():
        mappings = parse_bridge_turtle(b_path, bridge_vocab="loc-relators")
        for b in mappings:
            src = b["source_uri"]
            tgt = b["target_uri"]
            notation = b.get("notation")
            code = notation or (
                src.rstrip("/").split("/")[-1].lower() if "relators/" in src else None
            )
            if not code:
                continue
            code = code.lower()
            res[tgt.lower()] = code
            tgt_token = tgt.rstrip("/").split(":")[-1].split("/")[-1].split("#")[-1].lower()
            res[tgt_token] = code
            res[src.lower()] = code
            src_token = src.rstrip("/").split(":")[-1].split("/")[-1].split("#")[-1].lower()
            res[src_token] = code

    if bridge_file is None and vocab_store is None:
        _CACHED_BRIDGED_ROLE_MAP = res
    return res


__all__ = [
    "get_bridged_role_map",
    "get_bundled_bridge_dir",
    "get_bundled_bridge_files",
    "parse_bridge_turtle",
    "resolve_bridge_files",
]
