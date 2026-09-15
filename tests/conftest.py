"""Shared test fixtures for centralized database initialization.

All test databases, vector indices, and storage files are created and
initialized inside pytest's ``tmp_path`` so that tests never touch the
project's default ``data/`` directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from reconciliator.config import StorageConfig


def init_test_storage(
    base_dir: Path | str,
    *,
    authority_db: Path | str | None = None,
    vocab_db: Path | str | None = None,
    progress_db: Path | str | None = None,
    work_index: Path | str | None = None,
    reset: bool = True,
) -> StorageConfig:
    """Centralized database initialization inside a temporary directory.

    Initializes all database schemas (authority, vocab, progress, corpus)
    and vector indices within `base_dir` so that tests never touch the
    project's default ``data/`` location.
    """
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    storage = StorageConfig(
        data_dir=base,
        authority_db=Path(authority_db) if authority_db else None,
        vocab_db=Path(vocab_db) if vocab_db else None,
        progress_db=Path(progress_db) if progress_db else None,
        work_index=Path(work_index) if work_index else None,
    )
    if reset:
        from reconciliator.db import reset_all_storage

        reset_all_storage(storage)
    return storage


@pytest.fixture
def tmp_storage(tmp_path: Path) -> StorageConfig:
    """Return an initialized StorageConfig with all databases inside tmp_path."""
    return init_test_storage(tmp_path)


@pytest.fixture
def tmp_storage_yaml(tmp_path: Path) -> str:
    """Return a YAML ``storage:`` block directing all databases to tmp_path."""
    return f'storage:\n  data_dir: "{tmp_path}"\n'


@pytest.fixture
def make_config_file(tmp_path: Path):
    """Factory fixture: create a config YAML file with storage in tmp_path.

    Returns a callable that accepts optional extra config sections as a dict
    and writes them to a YAML file alongside a ``storage.data_dir`` pointing
    to ``tmp_path``.

    Usage::

        def test_example(make_config_file):
            cfg_path = make_config_file({"embedding": {"device": "cpu"}})
    """

    def _make(extra_sections: dict[str, Any] | None = None) -> Path:
        data: dict[str, Any] = {"storage": {"data_dir": str(tmp_path)}}
        if extra_sections:
            data.update(extra_sections)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.dump(data, default_flow_style=False), encoding="utf-8"
        )
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
        work_uri = rec.work_uri or f"urn:work:{origin}:{rid}"
        lang = rec.language or "und"
        expr_uri = rec.expression_uri or f"urn:exp:{origin}:{rid}:{lang}"
        manif_uri = rec.manifestation_uri or f"urn:manif:{origin}:{rid}"

        works.append(
            WorkEntity(
                work_uri=work_uri, source=origin, source_id=rid, record_origin=origin,
                title=rec.uniform_title or rec.title, title_normalized=rec.title_stem,
                work_type=rec.work_type, serialized_text=rec.serialized_work,
            )
        )
        exprs.append(
            ExpressionEntity(
                work_id=0, expression_uri=expr_uri, source=origin, source_id=rid,
                record_origin=origin, language=lang,
                content_type=rec.content_type or "text", title=rec.title,
                serialized_text=rec.serialized_expression,
            )
        )
        manifs.append(
            ManifestationEntity(
                manifestation_uri=manif_uri, source=origin, source_id=rid,
                record_origin=origin, title=rec.title, title_normalized=rec.title_stem,
                publication_year=rec.year, volume_designation=rec.volume_designation,
                is_component=rec.is_component, serialized_text=rec.serialized_manifestation,
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
                        entity_type="manifestation", entity_id=0, key_type=key_type,
                        key_value=value, entity_uri=manif_uri,
                    )
                )
        for scheme, values in (rec.identifiers or {}).items():
            for value in values:
                idents.append(
                    IdentifierEntity(
                        entity_type="manifestation", entity_id=0, scheme=scheme,
                        value=value, value_normalized=value.lower(), entity_uri=manif_uri,
                    )
                )

    repo.register_source(origin, is_authoritative=False)
    repo.insert_wemi_batch(
        works=works, expressions=exprs, manifestations=manifs,
        identifiers=idents, entity_keys=keys, record_origin=origin,
    )
    return len(records)
