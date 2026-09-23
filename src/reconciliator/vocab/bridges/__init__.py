"""Bridge vocabulary discovery and parsing.

Bridges foreign metadata vocabularies (e.g. LoC country codes, LoC language codes,
LoC relator codes, mode of issuance) to canonical ontologies via skos:exactMatch.
"""

from __future__ import annotations

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
    """Parse bridge Turtle content into structured bridge mappings using rdflib.

    Extracts:
    - source_uri: e.g. <http://id.loc.gov/vocabulary/countries/fi>
    - target_uri: e.g. <http://www.yso.fi/onto/yso/p94426>
    - notation: e.g. "fi"
    - pref_labels: dict[lang, label]
    - alt_labels: dict[lang, list[label]]
    """
    if isinstance(content, Path):
        content = content.read_text(encoding="utf-8")

    import rdflib
    from rdflib.namespace import OWL, RDFS, SKOS

    g = rdflib.Graph()
    g.parse(data=content, format="turtle")

    bridges: list[dict[str, Any]] = []

    # Find subjects that have skos:exactMatch or owl:sameAs
    seen_subjects = list(dict.fromkeys(
        list(g.subjects(SKOS.exactMatch, None)) + list(g.subjects(OWL.sameAs, None))
    ))

    for subj in seen_subjects:
        subj_uri = str(subj)
        target_objs = list(g.objects(subj, SKOS.exactMatch)) + list(g.objects(subj, OWL.sameAs))
        if not target_objs:
            continue
        target_uri = str(target_objs[0])

        pref_labels: dict[str, str] = {}
        for pref_obj in g.objects(subj, SKOS.prefLabel):
            lang = (getattr(pref_obj, "language", None) or "und").lower()
            pref_labels[lang] = str(pref_obj)

        # Fallback to rdfs:label if no prefLabel found
        if not pref_labels:
            for rdfs_obj in g.objects(subj, RDFS.label):
                lang = (getattr(rdfs_obj, "language", None) or "und").lower()
                pref_labels[lang] = str(rdfs_obj)

        alt_labels: dict[str, list[str]] = {}
        for alt_obj in g.objects(subj, SKOS.altLabel):
            lang = (getattr(alt_obj, "language", None) or "und").lower()
            alt_labels.setdefault(lang, []).append(str(alt_obj))

        notation_obj = g.value(subj, SKOS.notation)
        notation = str(notation_obj) if notation_obj is not None else None

        item: dict[str, Any] = {
            "source_uri": subj_uri,
            "target_uri": target_uri,
            "bridge_vocab": bridge_vocab,
            "notation": notation,
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
