"""Controlled vocabulary and migration tables ingestion pipeline into dedicated vocab.db."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional, Sequence

from reconciliator._core.parsers import (
    py_parse_turtle_content,
    py_parse_turtle_file,
)
from sqlalchemy.exc import DataError

from reconciliator.config import (
    VocabIngestConfig,
    get_bundled_vocabularies_defaults,
    get_category_default_field_map,
)
from reconciliator.database.schema import (
    concept_aliases_table,
    concept_hierarchy_table,
    relator_terms_table,
)
from reconciliator.ingestion.sanitizer import (
    sanitize_entities,
    sanitize_vocab_concepts,
    sanitize_vocab_mappings,
)
from reconciliator.models import ConceptMapping, VocabConcept
from reconciliator.normalizers.linguistic import fold_label
from reconciliator.progress import ProgressTracker
from reconciliator.vocab.bridges import (
    parse_bridge_turtle,
    resolve_bridge_files,
)
from reconciliator.vocab.store import VocabStore

logger = logging.getLogger(__name__)

_DEFAULT_LEGACY_SOURCES: set[str] = set(
    (get_bundled_vocabularies_defaults().get("legacy_to_modern") or {}).keys()
)


def parse_turtle_simple(
    content: str,
    pref_label_languages: Optional[Sequence[str] | str] = None,
    alt_label_languages: Optional[Sequence[str] | str] = None,
    vocab_target_fields: Optional[dict[str, str]] = None,
    legacy_thesauri: Optional[set[str]] = None,
    default_vocab: Optional[str] = None,
) -> tuple[list[VocabConcept], list[ConceptMapping], list[dict[str, Any]]]:
    """W3C-compliant SKOS Turtle (.ttl) parser backed by streaming native Rust parser.

    Extracts skos:Concept, prefLabels, altLabels, exactMatch, isReplacedBy, skos:broader, and skos:narrower.
    """
    active_legacy = (
        list(legacy_thesauri) if legacy_thesauri is not None else list(_DEFAULT_LEGACY_SOURCES)
    )
    active_target_fields = (
        vocab_target_fields
        if vocab_target_fields is not None
        else get_bundled_vocabularies_defaults().get("vocab_target_fields", {})
    )
    default_target_field = get_bundled_vocabularies_defaults().get(
        "default_vocab_target_field", "650"
    )
    pref_langs = (
        [pref_label_languages]
        if isinstance(pref_label_languages, str)
        else list(pref_label_languages)
        if pref_label_languages is not None
        else None
    )
    alt_langs = (
        [alt_label_languages]
        if isinstance(alt_label_languages, str)
        else list(alt_label_languages)
        if alt_label_languages is not None
        else None
    )
    custom_pats = get_bundled_vocabularies_defaults().get("vocabulary_patterns", {})

    rcs, rms, res = py_parse_turtle_content(
        content,
        default_vocab=default_vocab,
        legacy_thesauri=active_legacy,
        vocab_target_fields=active_target_fields,
        default_target_field=default_target_field,
        pref_langs=pref_langs,
        alt_langs=alt_langs,
        custom_patterns=custom_pats,
    )
    concepts = [
        VocabConcept.model_construct(
            vocabulary=rc.vocabulary,
            concept_id=rc.concept_id,
            pref_labels=rc.pref_labels,
            alt_labels=rc.alt_labels,
            is_deprecated=rc.is_deprecated,
            target_field=rc.target_field,
        )
        for rc in rcs
    ]
    mappings = [
        ConceptMapping.model_construct(
            legacy_vocab=rm.legacy_vocab,
            legacy_id=rm.legacy_id,
            target_vocab=rm.target_vocab,
            target_id=rm.target_id,
            mapping_type=rm.mapping_type,
            target_field=rm.target_field,
            legacy_labels=rm.legacy_labels,
        )
        for rm in rms
    ]
    hierarchy_edges = [
        {
            "vocabulary": edge.vocabulary,
            "concept_id": edge.concept_id,
            "broader_concept_id": edge.broader_concept_id,
            "relation_type": edge.relation_type,
        }
        for edge in res
    ]
    return concepts, mappings, hierarchy_edges


def parse_turtle_file(
    file_path: Path | str,
    pref_label_languages: Optional[Sequence[str] | str] = None,
    alt_label_languages: Optional[Sequence[str] | str] = None,
    vocab_target_fields: Optional[dict[str, str]] = None,
    legacy_thesauri: Optional[set[str]] = None,
    default_vocab: Optional[str] = None,
) -> tuple[list[VocabConcept], list[ConceptMapping], list[dict[str, Any]]]:
    """Parse Turtle file using streaming native Rust parser."""
    active_legacy = (
        list(legacy_thesauri) if legacy_thesauri is not None else list(_DEFAULT_LEGACY_SOURCES)
    )
    active_target_fields = (
        vocab_target_fields
        if vocab_target_fields is not None
        else get_bundled_vocabularies_defaults().get("vocab_target_fields", {})
    )
    default_target_field = get_bundled_vocabularies_defaults().get(
        "default_vocab_target_field", "650"
    )
    pref_langs = (
        [pref_label_languages]
        if isinstance(pref_label_languages, str)
        else list(pref_label_languages)
        if pref_label_languages is not None
        else None
    )
    alt_langs = (
        [alt_label_languages]
        if isinstance(alt_label_languages, str)
        else list(alt_label_languages)
        if alt_label_languages is not None
        else None
    )
    custom_pats = get_bundled_vocabularies_defaults().get("vocabulary_patterns", {})

    rcs, rms, res = py_parse_turtle_file(
        str(file_path),
        default_vocab=default_vocab,
        legacy_thesauri=active_legacy,
        vocab_target_fields=active_target_fields,
        default_target_field=default_target_field,
        pref_langs=pref_langs,
        alt_langs=alt_langs,
        custom_patterns=custom_pats,
    )
    concepts = [
        VocabConcept.model_construct(
            vocabulary=rc.vocabulary,
            concept_id=rc.concept_id,
            pref_labels=rc.pref_labels,
            alt_labels=rc.alt_labels,
            is_deprecated=rc.is_deprecated,
            target_field=rc.target_field,
        )
        for rc in rcs
    ]
    mappings = [
        ConceptMapping.model_construct(
            legacy_vocab=rm.legacy_vocab,
            legacy_id=rm.legacy_id,
            target_vocab=rm.target_vocab,
            target_id=rm.target_id,
            mapping_type=rm.mapping_type,
            target_field=rm.target_field,
            legacy_labels=rm.legacy_labels,
        )
        for rm in rms
    ]
    hierarchy_edges = [
        {
            "vocabulary": edge.vocabulary,
            "concept_id": edge.concept_id,
            "broader_concept_id": edge.broader_concept_id,
            "relation_type": edge.relation_type,
        }
        for edge in res
    ]
    return concepts, mappings, hierarchy_edges


def ingest_bridge_files(
    vocab_store: VocabStore,
    extra_bridge_dir: Optional[Path] = None,
    config: Optional[VocabIngestConfig] = None,
    tracker: Optional[ProgressTracker] = None,
) -> int:
    """Ingest bundled and optional custom bridge files into concept_mappings and concept_aliases."""
    bridge_files = resolve_bridge_files(config)
    if extra_bridge_dir and extra_bridge_dir.is_dir():
        for bf in sorted(extra_bridge_dir.glob("*.ttl")):
            if bf not in bridge_files:
                bridge_files.append(bf)

    total_bridge_mappings: list[ConceptMapping] = []
    total_bridge_aliases: list[dict[str, Any]] = []
    total_bridge_relators: list[dict[str, Any]] = []

    cat_defaults = get_category_default_field_map()
    geo_field = cat_defaults["geographic"]
    topical_field = cat_defaults["topical"]
    vocab_defs = get_bundled_vocabularies_defaults()
    default_topical_vocab = (
        vocab_defs.get("subject_categories", {}).get(topical_field, {}).get("default_vocab") or ""
    )
    pref_langs = vocab_defs.get("preferred_label_languages") or []

    for bf in bridge_files:
        stem = bf.stem.replace(".ttl", "")
        content = bf.read_text(encoding="utf-8")
        bridges = parse_bridge_turtle(content, bridge_vocab=stem)
        target_field = (
            geo_field if "countries" in stem else ("100" if "relators" in stem else topical_field)
        )

        for b in bridges:
            source_uri = b["source_uri"]
            target_uri = b["target_uri"]
            from reconciliator.utils.curie import parse_curie

            s_src, _ = parse_curie(source_uri)
            legacy_v = s_src if s_src and s_src != "unknown" else stem.removesuffix("-bridge")
            t_src, _ = parse_curie(target_uri)
            target_v = t_src if t_src and t_src != "unknown" else default_topical_vocab

            legacy_labels: dict[str, str] = dict(b.get("pref_labels") or {})
            for lang_key in pref_langs:
                if b.get(f"pref_label_{lang_key}") and lang_key not in legacy_labels:
                    legacy_labels[lang_key] = b[f"pref_label_{lang_key}"]

            total_bridge_mappings.append(
                ConceptMapping(
                    legacy_uri=source_uri,
                    legacy_vocab=legacy_v,
                    legacy_labels=legacy_labels,
                    target_uri=target_uri,
                    target_vocab=target_v,
                    mapping_type="exact_match",
                    target_field=target_field,
                )
            )
            notation = b.get("notation")
            alt_labels: dict[str, list[str]] = dict(b.get("alt_labels") or {})

            if "relators" in stem:
                total_bridge_mappings.append(
                    ConceptMapping(
                        legacy_uri=target_uri,
                        legacy_vocab=target_v,
                        legacy_labels=legacy_labels,
                        target_uri=source_uri,
                        target_vocab="loc-relators",
                        mapping_type="exact_match",
                        target_field=target_field,
                    )
                )
                for lang, label in legacy_labels.items():
                    if label:
                        folded = fold_label(label)
                        if folded:
                            total_bridge_relators.append(
                                {
                                    "folded_term": folded,
                                    "raw_term": label,
                                    "lang": lang,
                                    "code": notation,
                                    "source_uri": source_uri,
                                    "bridged_uri": target_uri,
                                    "is_preferred": True,
                                }
                            )
                for lang, labels in alt_labels.items():
                    for label in labels:
                        if label:
                            folded = fold_label(label)
                            if folded:
                                total_bridge_relators.append(
                                    {
                                        "folded_term": folded,
                                        "raw_term": label,
                                        "lang": lang,
                                        "code": notation,
                                        "source_uri": source_uri,
                                        "bridged_uri": target_uri,
                                        "is_preferred": False,
                                    }
                                )
                if notation:
                    total_bridge_relators.append(
                        {
                            "folded_term": fold_label(notation),
                            "raw_term": notation,
                            "lang": "und",
                            "code": notation,
                            "source_uri": source_uri,
                            "bridged_uri": target_uri,
                            "is_preferred": False,
                        }
                    )

            for lang, label in legacy_labels.items():
                if label:
                    folded = fold_label(label)
                    if folded:
                        total_bridge_aliases.append(
                            {
                                "folded_label": folded,
                                "raw_label": label,
                                "lang": lang,
                                "concept_uri": target_uri,
                                "wikidata_qid": None,
                                "vocabulary": target_v,
                                "target_field": target_field,
                                "is_preferred": 1,
                                "source": "bridge",
                            }
                        )

            for lang, labels in alt_labels.items():
                for label in labels:
                    if label:
                        folded = fold_label(label)
                        if folded:
                            total_bridge_aliases.append(
                                {
                                    "folded_label": folded,
                                    "raw_label": label,
                                    "lang": lang,
                                    "concept_uri": target_uri,
                                    "wikidata_qid": None,
                                    "vocabulary": target_v,
                                    "target_field": target_field,
                                    "is_preferred": 0,
                                    "source": "bridge",
                                }
                            )

            if notation:
                total_bridge_aliases.append(
                    {
                        "folded_label": fold_label(notation),
                        "raw_label": notation,
                        "lang": "und",
                        "concept_uri": target_uri,
                        "wikidata_qid": None,
                        "vocabulary": target_v,
                        "target_field": target_field,
                        "is_preferred": 0,
                        "source": "bridge",
                    }
                )

    task_id = tracker.task_id if tracker and tracker.task_id else "vocab-bridge-ingest"
    if config and getattr(config, "truncate_overlong_values", True):
        sanitize_vocab_mappings(
            total_bridge_mappings, config=config, task_id=task_id, tracker=tracker
        )
        sanitize_entities(
            total_bridge_aliases,
            concept_aliases_table,
            config=config,
            task_id=task_id,
            tracker=tracker,
        )
        sanitize_entities(
            total_bridge_relators,
            relator_terms_table,
            config=config,
            task_id=task_id,
            tracker=tracker,
        )

    if total_bridge_mappings:
        try:
            vocab_store.insert_mappings(total_bridge_mappings)
        except DataError as err:
            if config and getattr(config, "truncate_overlong_values", True):
                from reconciliator.logging import log_validation

                log_validation(
                    logger,
                    action="skip_batch",
                    entity="bridge_mappings",
                    source_id="bridge_files",
                    reason=f"database DataError: {err}",
                    task_id=task_id,
                    tracker=tracker,
                )
            else:
                raise

    if total_bridge_aliases:
        try:
            vocab_store.insert_aliases(total_bridge_aliases)
        except DataError as err:
            if config and getattr(config, "truncate_overlong_values", True):
                from reconciliator.logging import log_validation

                log_validation(
                    logger,
                    action="skip_batch",
                    entity="bridge_aliases",
                    source_id="bridge_files",
                    reason=f"database DataError: {err}",
                    task_id=task_id,
                    tracker=tracker,
                )
            else:
                raise

    if total_bridge_relators:
        try:
            vocab_store.insert_relators(total_bridge_relators)
        except DataError as err:
            if config and getattr(config, "truncate_overlong_values", True):
                from reconciliator.logging import log_validation

                log_validation(
                    logger,
                    action="skip_batch",
                    entity="bridge_relators",
                    source_id="bridge_files",
                    reason=f"database DataError: {err}",
                    task_id=task_id,
                    tracker=tracker,
                )
            else:
                raise

    return len(total_bridge_mappings)


def ingest_vocab_file(
    input_path: Path | str,
    vocab_store: VocabStore,
    tracker: Optional[ProgressTracker] = None,
    config: Optional[VocabIngestConfig] = None,
) -> tuple[int, int]:
    """Ingest SKOS Turtle or mapping file(s) into vocab.db.

    Supports either a single file (.ttl) or a directory containing multiple .ttl files.
    """
    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"Vocabulary input path not found: {path}")

    files: list[Path] = []
    if path.is_dir():
        files = sorted(path.glob("*.ttl"))
        if not files:
            files = sorted(path.rglob("*.ttl"))
        if not files:
            raise FileNotFoundError(f"No .ttl files found in directory: {path}")
    else:
        files = [path]

    pref_langs = (
        config.pref_label_languages
        if config and config.pref_label_languages is not None
        else vocab_store.config.ingest_pref_label_languages
    )
    alt_langs = (
        config.alt_label_languages
        if config and config.alt_label_languages is not None
        else vocab_store.config.ingest_alt_label_languages
    )

    vocab_target_fields = getattr(config, "vocab_target_fields", None) or getattr(
        vocab_store.config, "vocab_target_fields", None
    )

    legacy_thesauri = set(
        config.legacy_thesauri
        if config and config.legacy_thesauri is not None
        else (
            vocab_store.config.legacy_to_modern.keys()
            if hasattr(vocab_store, "config")
            else _DEFAULT_LEGACY_SOURCES
        )
    )

    if tracker:
        tracker.start_task(stage="parsing_vocab")

    total_concepts: list[VocabConcept] = []
    total_mappings: list[ConceptMapping] = []
    total_hierarchy_edges: list[dict[str, Any]] = []

    for file_path in files:
        file_vocab = file_path.stem.lower()
        concepts, mappings, edges = parse_turtle_file(
            file_path,
            pref_label_languages=pref_langs,
            alt_label_languages=alt_langs,
            vocab_target_fields=vocab_target_fields,
            legacy_thesauri=legacy_thesauri,
            default_vocab=file_vocab,
        )
        total_concepts.extend(concepts)
        total_mappings.extend(mappings)
        total_hierarchy_edges.extend(edges)

    if tracker:
        tracker.update_progress(
            stage="inserting_concepts",
            items_total=len(total_concepts) + len(total_mappings) + len(total_hierarchy_edges),
            force=True,
        )

    task_id = tracker.task_id if tracker and tracker.task_id else "vocab-ingest"
    if config and getattr(config, "truncate_overlong_values", True):
        sanitize_vocab_concepts(total_concepts, config=config, task_id=task_id, tracker=tracker)
        sanitize_vocab_mappings(total_mappings, config=config, task_id=task_id, tracker=tracker)
        if total_hierarchy_edges:
            sanitize_entities(
                total_hierarchy_edges,
                concept_hierarchy_table,
                config=config,
                task_id=task_id,
                tracker=tracker,
            )

    try:
        vocab_store.insert_concepts(total_concepts)
    except DataError as err:
        if config and getattr(config, "truncate_overlong_values", True):
            from reconciliator.logging import log_validation

            log_validation(
                logger,
                action="skip_batch",
                entity="vocab_concepts",
                source_id="vocab_file",
                reason=f"database DataError: {err}",
                task_id=task_id,
                tracker=tracker,
            )
        else:
            raise

    try:
        vocab_store.insert_mappings(total_mappings)
    except DataError as err:
        if config and getattr(config, "truncate_overlong_values", True):
            from reconciliator.logging import log_validation

            log_validation(
                logger,
                action="skip_batch",
                entity="vocab_mappings",
                source_id="vocab_file",
                reason=f"database DataError: {err}",
                task_id=task_id,
                tracker=tracker,
            )
        else:
            raise

    if total_hierarchy_edges:
        try:
            vocab_store.insert_hierarchy_batch(total_hierarchy_edges)
        except DataError as err:
            if config and getattr(config, "truncate_overlong_values", True):
                from reconciliator.logging import log_validation

                log_validation(
                    logger,
                    action="skip_batch",
                    entity="vocab_hierarchy",
                    source_id="vocab_file",
                    reason=f"database DataError: {err}",
                    task_id=task_id,
                    tracker=tracker,
                )
            else:
                raise

    # Ingest MTS contributor roles into relator_terms
    mts_to_code: dict[str, str] = {}
    for m in total_mappings:
        if "id.loc.gov/vocabulary/relators/" in m.target_uri and (
            "mts:" in m.legacy_uri or "/mts/" in m.legacy_uri
        ):
            mts_to_code[m.legacy_uri] = m.target_uri.rstrip("/").split("/")[-1].lower()
        elif "id.loc.gov/vocabulary/relators/" in m.legacy_uri and (
            "mts:" in m.target_uri or "/mts/" in m.target_uri
        ):
            mts_to_code[m.target_uri] = m.legacy_uri.rstrip("/").split("/")[-1].lower()

    mts_roles: list[dict[str, Any]] = []
    for c in total_concepts:
        if c.vocabulary == "mts":
            code = mts_to_code.get(c.uri)
            for lang, lbl in c.pref_labels.items():
                if lbl:
                    folded = fold_label(lbl)
                    if folded:
                        mts_roles.append(
                            {
                                "folded_term": folded,
                                "raw_term": lbl,
                                "lang": lang,
                                "code": code,
                                "uri": c.uri,
                            }
                        )
    if mts_roles:
        if config and getattr(config, "truncate_overlong_values", True):
            sanitize_entities(
                mts_roles,
                relator_terms_table,
                config=config,
                task_id=task_id,
                tracker=tracker,
            )
        try:
            vocab_store.insert_relators(mts_roles)
        except DataError as err:
            if config and getattr(config, "truncate_overlong_values", True):
                from reconciliator.logging import log_validation

                log_validation(
                    logger,
                    action="skip_batch",
                    entity="vocab_relators",
                    source_id="vocab_file",
                    reason=f"database DataError: {err}",
                    task_id=task_id,
                    tracker=tracker,
                )
            else:
                raise

    # Ingest bundled bridge vocabularies
    ingest_bridge_files(vocab_store, config=config, tracker=tracker)

    # Rebuild normalized aliases index
    vocab_store.rebuild_aliases_from_concepts()

    if tracker:
        tracker.complete_task(
            metadata={
                "concepts_ingested": len(total_concepts),
                "mappings_ingested": len(total_mappings),
                "files_ingested": [f.name for f in files],
            }
        )

    return len(total_concepts), len(total_mappings)
