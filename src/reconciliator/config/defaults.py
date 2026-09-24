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
    """Get bundled default vocabulary definitions dictionary directly from defaults.yaml."""
    return get_bundled_section_defaults("vocabularies")


def get_bundled_record_defaults() -> dict[str, Any]:
    """Get bundled default record normalization dictionary directly from defaults.yaml."""
    return get_bundled_section_defaults("record_normalization")


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
