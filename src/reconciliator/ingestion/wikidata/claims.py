"""Wikidata claim inspection and property extraction utilities."""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.config import (
    WikidataIngestConfig,
    get_bundled_defaults,
    get_category_default_field_map,
)
from reconciliator.normalizers.linguistic import fold_label

WIKIDATA_LANG_MAP: dict[str, str] = dict(
    get_bundled_defaults().get("ingestion", {}).get("wikidata", {}).get("languages") or {}
)
WIKIDATA_PREF_LANGS: list[str] = list(
    get_bundled_defaults().get("vocabularies", {}).get("preferred_label_languages") or []
)

_DEFAULT_WIKIDATA_CONFIG = WikidataIngestConfig()
_HUMAN_AND_ENTITY_TYPES: set[str] = set(_DEFAULT_WIKIDATA_CONFIG.human_and_entity_types)
_CREATIVE_WORK_TYPES: set[str] = set(_DEFAULT_WIKIDATA_CONFIG.creative_work_types.keys())
_CREATIVE_WORK_MAPPING: dict[str, str] = dict(_DEFAULT_WIKIDATA_CONFIG.creative_work_types)
_BOOK_TYPES: set[str] = {"Q571", "Q7725634", "Q47461344", "Q3331189"}
_AUTHOR_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("P50", "aut"),
    ("P98", "edt"),
    ("P655", "trl"),
    ("P110", "ill"),
    ("P86", "cmp"),
    ("P767", "ctb"),
)


def get_claim_snak_value(claims: dict[str, Any], prop: str) -> Optional[Any]:
    """Extract first value from claims structure."""
    statements = claims.get(prop, [])
    if statements and isinstance(statements, list):
        mainsnak = statements[0].get("mainsnak", {})
        datavalue = mainsnak.get("datavalue", {})
        return datavalue.get("value")
    return None


def get_claim_qids(claims: dict[str, Any], prop: str) -> list[str]:
    """Extract target QIDs from property claims."""
    qids: list[str] = []
    for stmt in claims.get(prop, []):
        mainsnak = stmt.get("mainsnak", {})
        datavalue = mainsnak.get("datavalue", {})
        val = datavalue.get("value")
        if isinstance(val, dict) and "id" in val:
            qids.append(val["id"])
    return qids


def extract_claim_qualifier_string(
    claims: dict[str, Any], prop: str, qual_props: tuple[str, ...] = ("P1810", "P1932")
) -> Optional[str]:
    """Extract string qualifier from property statements (e.g. P1810 'stated as', P1932)."""
    for stmt in claims.get(prop, []):
        quals = stmt.get("qualifiers", {})
        for q_prop in qual_props:
            for q_stmt in quals.get(q_prop, []):
                val = q_stmt.get("datavalue", {}).get("value")
                if isinstance(val, str) and val.strip():
                    return val.strip()
                elif isinstance(val, dict) and "text" in val and str(val["text"]).strip():
                    return str(val["text"]).strip()
    return None


def is_qid(val: Any) -> bool:
    """Check if value is an unresolved Wikidata QID (e.g. 'Q12345')."""
    if not val:
        return False
    s = str(val).strip()
    return len(s) > 1 and s.startswith("Q") and s[1:].isdigit()


def extract_wikidata_yso_aliases(entity: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract alias records from a Wikidata entity with property P2347 (YSO ID)."""
    claims = entity.get("claims", {})
    if "P2347" not in claims:
        return []

    qid = entity.get("id", "")
    yso_id = get_claim_snak_value(claims, "P2347")
    if not yso_id:
        return []

    yso_str = str(yso_id).strip()
    yso_clean = yso_str.split("/")[-1] if "/" in yso_str else yso_str
    if yso_clean and not yso_clean.startswith("p"):
        yso_clean = "p" + yso_clean
    concept_id = yso_clean or qid
    concept_vocab = "yso" if yso_clean else "wikidata"

    instance_of = get_claim_qids(claims, "P31")
    is_place = any(
        k in instance_of for k in ("Q515", "Q6256", "Q532", "Q15284", "Q486970", "Q1048835")
    )
    cat_defaults = get_category_default_field_map()
    target_field = cat_defaults["geographic"] if is_place else cat_defaults["topical"]

    aliases: list[dict[str, Any]] = []
    labels = entity.get("labels", {})
    for lang, l_data in labels.items():
        val = l_data.get("value", "") if isinstance(l_data, dict) else str(l_data)
        if val and val.strip():
            folded = fold_label(val)
            if folded:
                aliases.append(
                    {
                        "folded_label": folded,
                        "raw_label": val.strip(),
                        "lang": lang,
                        "concept_id": concept_id,
                        "vocabulary": concept_vocab,
                        "target_field": target_field,
                        "is_preferred": 1,
                        "source": f"wikidata:{qid}" if qid else "wikidata",
                    }
                )

    all_aliases = entity.get("aliases", {})
    for lang, a_list in all_aliases.items():
        if isinstance(a_list, list):
            for a_item in a_list:
                val = a_item.get("value", "") if isinstance(a_item, dict) else str(a_item)
                if val and val.strip():
                    folded = fold_label(val)
                    if folded:
                        aliases.append(
                            {
                                "folded_label": folded,
                                "raw_label": val.strip(),
                                "lang": lang,
                                "concept_id": concept_id,
                                "vocabulary": concept_vocab,
                                "target_field": target_field,
                                "is_preferred": 0,
                                "source": f"wikidata:{qid}" if qid else "wikidata",
                            }
                        )

    return aliases
