"""Package defaults loader for Reconciliator configuration."""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any, Final, Literal

import yaml

_BUNDLED_DEFAULTS_YAML: Final[Path] = (
    Path(__file__).resolve().parent.parent / "defaults" / "defaults.yaml"
)


@functools.lru_cache(maxsize=1)
def get_bundled_defaults() -> dict[str, Any]:
    """Load default configuration from package-bundled defaults.yaml."""
    if _BUNDLED_DEFAULTS_YAML.exists():
        with _BUNDLED_DEFAULTS_YAML.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def get_bundled_section_defaults(section: str) -> dict[str, Any]:
    """Get bundled default settings dictionary for a top-level section."""
    return dict(get_bundled_defaults().get(section) or {})


def get_bundled_compute_defaults() -> dict[str, Any]:
    """Get bundled default compute settings dictionary."""
    return get_bundled_section_defaults("compute")


def get_bundled_storage_defaults() -> dict[str, Any]:
    """Get bundled default storage settings dictionary."""
    return get_bundled_section_defaults("storage")


def get_bundled_telemetry_defaults() -> dict[str, Any]:
    """Get bundled default telemetry settings dictionary."""
    return get_bundled_section_defaults("telemetry")


def get_bundled_logging_defaults() -> dict[str, Any]:
    """Get bundled default logging settings dictionary."""
    return get_bundled_section_defaults("logging")


def get_bundled_linguistic_defaults() -> dict[str, Any]:
    """Get bundled default linguistic rules dictionary."""
    return get_bundled_section_defaults("linguistic")


def get_bundled_sources_defaults() -> dict[str, Any]:
    """Get bundled default source definitions dictionary."""
    return get_bundled_section_defaults("sources")


def get_bundled_vocabularies_defaults() -> dict[str, Any]:
    """Get bundled default vocabulary definitions dictionary with derived mappings."""
    raw = dict(get_bundled_defaults().get("vocabularies") or {})
    sf = raw.get("subject_fields") or {}
    if "field_to_vocabularies" not in raw:
        raw["field_to_vocabularies"] = {k: list(v.get("allowed_vocabs", [])) for k, v in sf.items()}
    if "default_field_vocabularies" not in raw:
        raw["default_field_vocabularies"] = {
            k: str(v.get("default_vocab", "")) for k, v in sf.items()
        }
    if "vocab_target_fields" not in raw:
        raw["vocab_target_fields"] = {
            str(v.get("default_vocab", "")): k
            for k, v in sf.items()
            if v.get("category") != "topical"
        }
    if "subject_field_category_map" not in raw:
        raw["subject_field_category_map"] = {k: str(v.get("category", "")) for k, v in sf.items()}
    if "default_vocab_target_field" not in raw:
        raw["default_vocab_target_field"] = str(raw.get("default_vocab_target_field") or "")
    if "default_target_field" not in raw:
        raw["default_target_field"] = str(
            raw.get("default_target_field") or raw.get("default_vocab_target_field") or ""
        )
    if "default_subject_field" not in raw:
        raw["default_subject_field"] = str(
            raw.get("default_subject_field") or raw.get("default_vocab_target_field") or ""
        )
    if "supported_subject_vocabularies" not in raw:
        raw["supported_subject_vocabularies"] = list(raw.get("supported_subjects", []))
    if "vocabularies" not in raw:
        seen = set()
        vocabs = []
        for key in ("agent_priority", "subject_priority", "entity_vocabularies"):
            for val in raw.get(key, []):
                v_up = str(val).upper()
                if v_up not in seen:
                    seen.add(v_up)
                    vocabs.append(v_up)
        raw["vocabularies"] = vocabs
    ling = get_bundled_linguistic_defaults()
    if "preserved_diacritics" not in raw:
        raw["preserved_diacritics"] = str(ling.get("preserved_diacritics") or "")
    b_dates = ling.get("biographical_dates", {})
    if "biographical_date_prefixes" not in raw:
        raw["biographical_date_prefixes"] = list(b_dates.get("prefixes", []))
    if "biographical_era_bc" not in raw:
        raw["biographical_era_bc"] = list(b_dates.get("era_bc", []))
    if "biographical_era_ad" not in raw:
        raw["biographical_era_ad"] = list(b_dates.get("era_ad", []))
    if "biographical_century_suffixes" not in raw:
        raw["biographical_century_suffixes"] = list(b_dates.get("century_suffixes", []))
    if "biographical_date_pattern" not in raw:
        raw["biographical_date_pattern"] = b_dates.get("pattern")
    if "default_language" not in raw:
        raw["default_language"] = str(
            raw.get("default_language") or ling.get("default_language") or ""
        )
    return raw


def get_bundled_record_defaults() -> dict[str, Any]:
    """Get bundled default record normalization dictionary with enrichments and linguistic defaults."""
    data = get_bundled_defaults()
    raw = dict(data.get("record_normalization") or data.get("record") or {})
    enrich = dict(data.get("enrichment") or {})
    if "marc_predicate_profiles" not in raw and "predicate_profiles" in enrich:
        raw["marc_predicate_profiles"] = enrich["predicate_profiles"]
    if "marc_schemes" not in raw and "schemes" in enrich:
        raw["marc_schemes"] = enrich["schemes"]
    if "marc_domain_concepts" not in raw and "domain_concepts" in enrich:
        raw["marc_domain_concepts"] = enrich["domain_concepts"]
    if "marc_linking_labels" not in raw and "linking_labels" in enrich:
        raw["marc_linking_labels"] = enrich["linking_labels"]
    if "marc_accessibility" not in raw and "accessibility" in enrich:
        raw["marc_accessibility"] = enrich["accessibility"]
    if "thesis_institution_uri" not in raw and "thesis_institution_uri" in enrich:
        raw["thesis_institution_uri"] = enrich["thesis_institution_uri"]
    if "thesis_relator_code" not in raw and "thesis_relator_code" in enrich:
        raw["thesis_relator_code"] = enrich["thesis_relator_code"]
    if "surrogate_title_keywords" not in raw and "surrogate_title_keywords" in enrich:
        raw["surrogate_title_keywords"] = enrich["surrogate_title_keywords"]
    if "ephemera_terms" not in raw and "ephemera_terms" in enrich:
        raw["ephemera_terms"] = enrich["ephemera_terms"]

    ling = get_bundled_linguistic_defaults()
    if "preserved_diacritics" not in raw:
        raw["preserved_diacritics"] = str(ling.get("preserved_diacritics") or "")
    b_dates = ling.get("biographical_dates", {})
    if "biographical_date_prefixes" not in raw:
        raw["biographical_date_prefixes"] = list(b_dates.get("prefixes", []))
    if "biographical_era_bc" not in raw:
        raw["biographical_era_bc"] = list(b_dates.get("era_bc", []))
    if "biographical_era_ad" not in raw:
        raw["biographical_era_ad"] = list(b_dates.get("era_ad", []))
    if "biographical_century_suffixes" not in raw:
        raw["biographical_century_suffixes"] = list(b_dates.get("century_suffixes", []))
    if "biographical_date_pattern" not in raw:
        raw["biographical_date_pattern"] = b_dates.get("pattern")
    if "default_language" not in raw:
        raw["default_language"] = str(ling.get("default_language") or "")

    vocab = dict(data.get("vocabularies") or {})
    sf = vocab.get("subject_fields") or {}
    if "subject_field_category_map" not in raw:
        raw["subject_field_category_map"] = {k: str(v.get("category", "")) for k, v in sf.items()}
    if "default_subject_field" not in raw:
        raw["default_subject_field"] = str(
            vocab.get("default_subject_field") or vocab.get("default_vocab_target_field") or ""
        )
    return raw


def get_bundled_normalization_defaults() -> dict[str, Any]:
    """Get bundled default record normalization dictionary."""
    return get_bundled_record_defaults()


def get_bundled_enrichment_defaults() -> dict[str, Any]:
    """Get bundled default enrichment settings dictionary."""
    return get_bundled_section_defaults("enrichment")


def get_bundled_virtual_authority_defaults() -> dict[str, Any]:
    """Get bundled default virtual authority settings dictionary."""
    return get_bundled_section_defaults("virtual_authority")


def get_bundled_bruno_defaults() -> dict[str, Any]:
    """Get bundled default Bruno test suite settings dictionary."""
    return get_bundled_section_defaults("bruno")


def get_bundled_agent_authority_defaults() -> dict[str, Any]:
    """Get bundled default agent authority settings dictionary."""
    return get_bundled_section_defaults("agent_authority")


def get_bundled_embedding_defaults() -> dict[str, Any]:
    """Get bundled default embedding model settings dictionary."""
    return get_bundled_section_defaults("embedding")


def get_bundled_llm_defaults() -> dict[str, Any]:
    """Get bundled default LLM settings dictionary."""
    return get_bundled_section_defaults("llm")


def get_bundled_vector_model_default() -> Literal["embedding", "llm"]:
    """Get bundled default vector model selection ('embedding' or 'llm')."""
    return str(get_bundled_defaults().get("vector_model") or "embedding")  # type: ignore[return-value]


def get_bundled_ingestion_defaults() -> dict[str, Any]:
    """Get bundled default ingestion settings dictionary."""
    return get_bundled_section_defaults("ingestion")


def get_bundled_oai_ingest_defaults() -> dict[str, Any]:
    """Get bundled default OAI-PMH ingestion settings dictionary."""
    ing = get_bundled_ingestion_defaults()
    return dict(ing.get("oai_defaults") or {})


def get_bundled_agent_clustering_defaults() -> dict[str, Any]:
    """Get bundled default agent clustering settings dictionary."""
    return get_bundled_section_defaults("agent_clustering")


def get_bundled_funnels_defaults() -> dict[str, Any]:
    """Get bundled default funnels settings dictionary."""
    return get_bundled_section_defaults("funnels")


def get_bundled_constituents_defaults() -> dict[str, Any]:
    """Get bundled default constituents settings dictionary."""
    return get_bundled_section_defaults("constituents")


def get_bundled_consolidation_defaults() -> dict[str, Any]:
    """Get bundled default consolidation settings dictionary."""
    return get_bundled_section_defaults("consolidation")


def get_bundled_reconcile_defaults() -> dict[str, Any]:
    """Get bundled default reconcile settings dictionary."""
    return get_bundled_section_defaults("reconcile")


def get_bundled_postgres_defaults() -> dict[str, Any]:
    """Get bundled default PostgreSQL storage settings dictionary."""
    storage = get_bundled_storage_defaults()
    return dict(storage.get("postgres") or {})


def get_default_database_url() -> str:
    """Get default PostgreSQL database URL constructed from bundled configuration defaults."""
    env_url = os.environ.get("RECONCILIATOR_DATABASE_URL")
    if env_url:
        return env_url
    pg = get_bundled_postgres_defaults()
    cfg_url = pg.get("database_url")
    if cfg_url:
        return cfg_url
    user = pg.get("user", "reconciliator")
    password = pg.get("password", "reconciliator")
    host = pg.get("host", "localhost")
    port = pg.get("port", 5432)
    database = pg.get("database", "reconciliator")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"
