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
    corpus_db: Path | str | None = None,
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
        corpus_db=Path(corpus_db) if corpus_db else None,
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
