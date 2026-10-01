"""SQLAlchemy 2.0 PostgreSQL store for controlled vocabularies, ontologies, and migration mappings."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import (
    Engine,
    select,
)

from reconciliator.config import LinguisticConfig, VocabulariesConfig
from reconciliator.database import (
    concept_aliases_table,
    concept_labels_table,
    concepts_table,
    get_engine,
)
from reconciliator.database import (
    metadata as vocab_metadata,
)
from reconciliator.models import VocabConcept
from reconciliator.utils.curie import to_curie
from reconciliator.vocab.detection import (
    concept_id_to_uri,
)
from reconciliator.vocab.mixins import (
    AliasStoreMixin,
    ConceptStoreMixin,
    HierarchyStoreMixin,
    LegacyMappingMixin,
    RelatorStoreMixin,
    SemanticVocabMixin,
)

logger = logging.getLogger(__name__)


def init_vocab_db(engine: Any) -> None:
    """Initialize concepts, concept_labels, concept_mappings, relator_terms, concept_aliases, and concept_hierarchy tables."""
    from reconciliator.database import create_schema_tables

    create_schema_tables(engine, vocab_metadata.sorted_tables)


class VocabMemoryIndex:
    """Process-level shared in-memory index for a specific database."""

    def __init__(self) -> None:
        self.concepts_by_id: dict[tuple[str, str], VocabConcept] = {}
        self.concepts_by_uri: dict[str, VocabConcept] = {}
        self.pref_labels: dict[tuple[str, Optional[str], Optional[str]], tuple[str, str]] = {}
        self.alias_index: dict[tuple[str, Optional[str]], tuple[str, str, str]] = {}
        self.is_preloaded: bool = False
        self.lock = threading.Lock()

    def clear(self) -> None:
        with self.lock:
            self.concepts_by_id.clear()
            self.concepts_by_uri.clear()
            self.pref_labels.clear()
            self.alias_index.clear()
            self.is_preloaded = False


_SHARED_INDEX_REGISTRY: dict[str, VocabMemoryIndex] = {}
_REGISTRY_LOCK = threading.Lock()


def get_shared_vocab_index(db_path: str) -> VocabMemoryIndex:
    """Get or create process-level shared in-memory index for a database URL/path."""
    with _REGISTRY_LOCK:
        if db_path not in _SHARED_INDEX_REGISTRY:
            _SHARED_INDEX_REGISTRY[db_path] = VocabMemoryIndex()
        return _SHARED_INDEX_REGISTRY[db_path]


class VocabStore(
    ConceptStoreMixin,
    LegacyMappingMixin,
    HierarchyStoreMixin,
    RelatorStoreMixin,
    AliasStoreMixin,
    SemanticVocabMixin,
):
    """Store for controlled vocabularies backed by SQLAlchemy 2.0 Engine."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        config: Optional[VocabulariesConfig] = None,
        engine: Optional[Engine] = None,
        linguistic: Optional[LinguisticConfig] = None,
    ) -> None:
        self.config = config or VocabulariesConfig()
        self.linguistic = linguistic or LinguisticConfig()
        if engine is not None:
            self.engine = engine
            self.db_path = (
                str(db_path)
                if db_path is not None
                else engine.url.render_as_string(hide_password=False)
            )
        else:
            self.engine = get_engine(db_path)
            self.db_path = (
                str(db_path)
                if db_path is not None
                else self.engine.url.render_as_string(hide_password=False)
            )
        init_vocab_db(self.engine)
        self._index = get_shared_vocab_index(self.db_path)
        if getattr(self.config, "preload", None) and self.config.preload.enabled:
            self.preload()

    def preload(self) -> None:
        """Preload controlled vocabulary concepts, preferred labels, and aliases into shared memory."""
        preload_cfg = getattr(self.config, "preload", None)

        if self._index.is_preloaded:
            return

        with self._index.lock:
            if self._index.is_preloaded:
                return

            load_concepts = preload_cfg.preload_concepts if preload_cfg else True
            load_pref = preload_cfg.preload_pref_labels if preload_cfg else True
            load_aliases = preload_cfg.preload_aliases if preload_cfg else True

            with self.engine.connect() as conn:
                if load_concepts or load_pref:
                    stmt = (
                        select(
                            concepts_table.c.vocabulary,
                            concepts_table.c.concept_id,
                            concepts_table.c.is_deprecated,
                            concepts_table.c.target_field,
                            concept_labels_table.c.lang,
                            concept_labels_table.c.label,
                        )
                        .select_from(
                            concepts_table.join(
                                concept_labels_table,
                                concepts_table.c.id == concept_labels_table.c.concept_row_id,
                            )
                        )
                        .where(concept_labels_table.c.label_type == "pref")
                    )
                    rows = conn.execute(stmt).fetchall()
                    for voc, cid, is_dep, tf, lang, lbl in rows:
                        voc_clean = str(voc).strip().lower()
                        cid_clean = str(cid).strip()
                        lbl_clean = lbl.strip().lower() if lbl else ""
                        l_clean = lang.strip().lower() if lang else ""
                        norm_l = self.config.language_code_map.get(l_clean, l_clean)

                        key = (voc_clean, cid_clean)
                        if key not in self._index.concepts_by_id:
                            concept = VocabConcept(
                                uri=concept_id_to_uri(to_curie(voc_clean, cid_clean))
                                or to_curie(voc_clean, cid_clean),
                                vocabulary=voc_clean,
                                concept_id=cid_clean,
                                pref_labels={l_clean: lbl} if lbl else {},
                                alt_labels={},
                                is_deprecated=bool(is_dep),
                                target_field=tf,
                            )
                            self._index.concepts_by_id[key] = concept
                            if concept.uri:
                                self._index.concepts_by_uri[concept.uri] = concept
                        else:
                            concept = self._index.concepts_by_id[key]
                            if lbl and l_clean not in concept.pref_labels:
                                concept.pref_labels[l_clean] = lbl

                        if load_pref and lbl_clean:
                            target_tuple = (voc_clean, cid_clean)
                            self._index.pref_labels[(lbl_clean, voc_clean, norm_l)] = target_tuple
                            if (lbl_clean, voc_clean, None) not in self._index.pref_labels:
                                self._index.pref_labels[(lbl_clean, voc_clean, None)] = target_tuple
                            if (lbl_clean, None, norm_l) not in self._index.pref_labels:
                                self._index.pref_labels[(lbl_clean, None, norm_l)] = target_tuple
                            if (lbl_clean, None, None) not in self._index.pref_labels:
                                self._index.pref_labels[(lbl_clean, None, None)] = target_tuple

                if load_aliases:
                    alias_stmt = (
                        select(
                            concept_aliases_table.c.folded_label,
                            concept_aliases_table.c.target_field,
                            concept_aliases_table.c.vocabulary,
                            concept_aliases_table.c.concept_id,
                            concept_aliases_table.c.source,
                        )
                        .distinct(
                            concept_aliases_table.c.folded_label,
                            concept_aliases_table.c.target_field,
                        )
                        .order_by(
                            concept_aliases_table.c.folded_label,
                            concept_aliases_table.c.target_field,
                            concept_aliases_table.c.is_preferred.desc(),
                            concept_aliases_table.c.id.asc(),
                        )
                    )
                    alias_rows = conn.execute(alias_stmt).fetchall()
                    for f_lbl, tf, voc, cid, src in alias_rows:
                        if not f_lbl or not cid:
                            continue
                        f_clean = str(f_lbl).strip()
                        tf_clean = str(tf).strip() if tf else None
                        voc_clean = str(voc).strip().lower()
                        cid_clean = str(cid).strip()
                        src_clean = str(src).strip() if src else ""
                        self._index.alias_index[(f_clean, tf_clean)] = (
                            voc_clean,
                            cid_clean,
                            src_clean,
                        )
                        if (f_clean, None) not in self._index.alias_index:
                            self._index.alias_index[(f_clean, None)] = (
                                voc_clean,
                                cid_clean,
                                src_clean,
                            )

            self._index.is_preloaded = True

    def get_connection(self) -> Any:
        """Get a configured connection context manager backed by the SQLAlchemy Engine."""
        return self.engine.begin()

    def close(self) -> None:
        """Dispose SQLAlchemy engine and release connection pool."""
        self.engine.dispose()

    def clear_caches(self) -> None:
        """Clear all in-memory LRU caches and shared index."""
        self._index.clear()
        if hasattr(self.find_concept_by_id, "cache_clear"):
            self.find_concept_by_id.cache_clear()
        if hasattr(self.find_concept_by_uri, "cache_clear"):
            self.find_concept_by_uri.cache_clear()
        if hasattr(self.find_concept_by_label, "cache_clear"):
            self.find_concept_by_label.cache_clear()
        if hasattr(self.find_legacy_mapping, "cache_clear"):
            self.find_legacy_mapping.cache_clear()
        if hasattr(self.find_legacy_mapping_by_uri, "cache_clear"):
            self.find_legacy_mapping_by_uri.cache_clear()
        if hasattr(self.find_legacy_mapping_by_label, "cache_clear"):
            self.find_legacy_mapping_by_label.cache_clear()
        if hasattr(self.is_known_relator, "cache_clear"):
            self.is_known_relator.cache_clear()
        if hasattr(self.get_relator_code, "cache_clear"):
            self.get_relator_code.cache_clear()
        if hasattr(self.get_canonical_relator_term, "cache_clear"):
            self.get_canonical_relator_term.cache_clear()
        if hasattr(self.get_relator_source_uri, "cache_clear"):
            self.get_relator_source_uri.cache_clear()
        if hasattr(self.get_relator_bridged_uri, "cache_clear"):
            self.get_relator_bridged_uri.cache_clear()
        if hasattr(self.find_concept_by_alias, "cache_clear"):
            self.find_concept_by_alias.cache_clear()
        if hasattr(self.get_broader_concepts, "cache_clear"):
            self.get_broader_concepts.cache_clear()
        if hasattr(self.get_narrower_concepts, "cache_clear"):
            self.get_narrower_concepts.cache_clear()
        if hasattr(self.load_hierarchy_edges, "cache_clear"):
            self.load_hierarchy_edges.cache_clear()

