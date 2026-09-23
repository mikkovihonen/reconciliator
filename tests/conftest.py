"""Shared test fixtures for centralized database initialization.

All test databases, vector indices, and storage files are created and
initialized inside pytest's ``tmp_path`` so that tests never touch the
project's default ``data/`` directory.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from reconciliator.config import StorageConfig
from reconciliator.database import get_engine, init_authority_db, init_corpus_db
from reconciliator.progress import init_progress_db
from reconciliator.vocab import init_vocab_db

os.environ.setdefault("RECONCILIATOR_NULL_POOL", "1")

logger = logging.getLogger(__name__)


@pytest.fixture(scope="session", autouse=True)
def ensure_postgres_container():
    """Ensure PostgreSQL container or instance is running."""
    from reconciliator.database import start_postgres_container, wait_for_postgres

    if not wait_for_postgres(timeout_seconds=0.5):
        try:
            start_postgres_container(timeout_seconds=30.0)
        except Exception as e:
            logger.warning("Could not auto-start postgres container: %s", e)


def init_test_storage(
    base_dir: Path | str,
    *,
    work_index: Path | str | None = None,
    reset: bool = True,
    schema: str | None = None,
) -> StorageConfig:
    """Centralized database initialization inside an isolated PostgreSQL schema."""
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)

    if schema is None:
        schema = f"test_{hashlib.md5(str(base).encode()).hexdigest()[:10]}"

    try:
        with psycopg.connect(
            "host=localhost port=5432 user=postgres password=postgres dbname=reconciliator"
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema};")
    except Exception as e:
        logger.debug("Failed to create test schema %s: %s", schema, e)

    pg_url = f"postgresql+psycopg://postgres:postgres@localhost:5432/reconciliator?options=-csearch_path%3D{schema},public"

    storage = StorageConfig(
        data_dir=base,
        database_url=pg_url,
        work_index=Path(work_index) if work_index else None,
    )

    if reset:
        engine = get_engine(pg_url)
        try:
            init_authority_db(engine)
            init_vocab_db(engine)
            init_corpus_db(engine)
            init_progress_db(engine)
        finally:
            engine.dispose()

    return storage


@pytest.fixture
def tmp_storage(tmp_path: Path) -> Generator[StorageConfig, None, None]:
    """Return an initialized StorageConfig with all databases inside isolated test schema."""
    schema = f"test_{hashlib.md5(str(tmp_path).encode()).hexdigest()[:10]}"
    storage = init_test_storage(tmp_path, schema=schema)
    yield storage
    try:
        with psycopg.connect(
            "host=localhost port=5432 user=postgres password=postgres dbname=reconciliator"
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE;")
    except Exception:
        pass


@pytest.fixture
def tmp_storage_yaml(tmp_storage: StorageConfig) -> str:
    """Return a YAML ``storage:`` block directing all databases to isolated test schema."""
    return f'storage:\n  data_dir: "{tmp_storage.data_dir}"\n  database_url: "{tmp_storage.database_url}"\n'


@pytest.fixture
def make_config_file(tmp_path: Path, tmp_storage: StorageConfig):
    """Factory fixture: create a config YAML file with storage in tmp_path.

    Returns a callable that accepts optional extra config sections as a dict
    and writes them to a YAML file alongside a ``storage.data_dir`` pointing
    to ``tmp_path``.

    Usage::

        def test_example(make_config_file):
            cfg_path = make_config_file({"embedding": {"device": "cpu"}})
    """

    def _make(extra_sections: dict[str, Any] | None = None) -> Path:
        data: dict[str, Any] = {
            "storage": {
                "data_dir": str(tmp_path),
                "database_url": tmp_storage.database_url,
            }
        }
        if extra_sections:
            data.update(extra_sections)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
        return cfg_file

    return _make


def persist_match_records(repo, records, origin: str = "corpus") -> int:
    """Persist MatchRecords as WEMI rows so the clusterer can project them back.

    The clusterer reads `get_all_match_records()`, which projects records out of
    works/expressions/manifestations plus `entity_keys`. There is no staging
    table any more, so a test that wants records in the store has to put them
    there the way ingestion does. Tests keep declaring the record shape they care
    about; this turns that shape into rows.
    """
    from reconciliator.models import (
        EntityKey,
        ExpressionEntity,
        IdentifierEntity,
        ManifestationEntity,
        WorkEntity,
    )

    works, exprs, manifs, keys, idents = [], [], [], [], []
    for rec in records:
        rid = rec.record_id
        curie = f"{origin}:{rid}"
        work_curie = rec.work_curie or curie
        lang = rec.language or "und"
        manif_curie = rec.manifestation_curie or curie

        works.append(
            WorkEntity(
                source=origin,
                source_id=rid,
                record_origin=origin,
                title=rec.uniform_title or rec.title,
                title_normalized=rec.title_stem,
                work_type=rec.work_type,
                serialized_text=rec.serialized_work,
            )
        )
        exprs.append(
            ExpressionEntity(
                work_id=0,
                work_curie=work_curie,
                source=origin,
                source_id=f"{rid}_{lang}",
                record_origin=origin,
                language=lang,
                content_type=rec.content_type or "text",
                title=rec.title,
                serialized_text=rec.serialized_expression,
            )
        )
        manifs.append(
            ManifestationEntity(
                work_curie=work_curie,
                source=origin,
                source_id=rid,
                record_origin=origin,
                title=rec.title,
                title_normalized=rec.title_stem,
                publication_year=rec.year,
                volume_designation=rec.volume_designation,
                is_component=rec.is_component,
                serialized_text=rec.serialized_manifestation,
            )
        )

        # The projection reads author_key, title_stem and uniform_title back out
        # of entity_keys, so they have to be written as keys, exactly as the
        # loaders do at ingest.
        bag = dict(rec.keys)
        if rec.author_key:
            bag.setdefault("author", [rec.author_key])
        if rec.title_stem:
            bag.setdefault("title_stem", [rec.title_stem])
        if rec.uniform_title:
            bag.setdefault("uniform_title", [rec.uniform_title])
        for key_type, values in bag.items():
            for value in values:
                keys.append(
                    EntityKey(
                        entity_type="manifestation",
                        entity_id=0,
                        key_type=key_type,
                        key_value=value,
                        entity_curie=manif_curie,
                    )
                )
        for scheme, values in (rec.identifiers or {}).items():
            for value in values:
                idents.append(
                    IdentifierEntity(
                        entity_type="manifestation",
                        entity_id=0,
                        source=scheme,
                        source_id=value,
                        source_id_normalized=value.lower(),
                        curie=f"{scheme}:{value}",
                        entity_curie=manif_curie,
                    )
                )

    repo.register_source(origin, is_authoritative=False)
    repo.insert_wemi_batch(
        works=works,
        expressions=exprs,
        manifestations=manifs,
        entity_keys=keys,
        identifiers=idents,
        record_origin=origin,
    )
    return len(records)
