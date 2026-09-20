"""Controlled vocabulary and migration tables ingestion pipeline into dedicated vocab.db."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy.exc import DataError

from reconciliator.config import (
    VocabIngestConfig,
    get_bundled_vocab_defaults,
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
from reconciliator.vocab.bridges import get_bundled_bridge_files, parse_bridge_turtle
from reconciliator.vocab.detection import extract_concept_id
from reconciliator.vocab.store import VocabStore, is_language_allowed

logger = logging.getLogger(__name__)



def parse_turtle_simple(
    content: str,
    pref_label_languages: Optional[Sequence[str] | str] = None,
    alt_label_languages: Optional[Sequence[str] | str] = None,
    vocab_target_fields: Optional[dict[str, str]] = None,
) -> tuple[list[VocabConcept], list[ConceptMapping], list[dict[str, Any]]]:
    """
    Fast regex-based parser for SKOS Turtle (.ttl) concepts, mappings, and hierarchy edges.
    Extracts skos:Concept, prefLabels, altLabels, exactMatch, isReplacedBy, skos:broader, and skos:narrower.
    Supports both full URIs (<...>) and prefixed IRIs (@prefix ...).
    """
    concepts: list[VocabConcept] = []
    mappings: list[ConceptMapping] = []
    hierarchy_edges: list[dict[str, Any]] = []

    # Extract prefix declarations
    prefixes: dict[str, str] = {}
    for m in re.finditer(
        r"(?:@prefix|PREFIX)\s+([a-zA-Z0-9_-]+:)\s+<([^>]+)>", content, re.IGNORECASE
    ):
        prefixes[m.group(1)] = m.group(2)

    def _resolve_uri(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("<") and raw.endswith(">"):
            return raw[1:-1]
        for pfx, base in prefixes.items():
            if raw.startswith(pfx):
                return base + raw[len(pfx) :]
        return raw

    # Match each skos:Concept block
    blocks = re.split(
        r"(<[^>]+>|[a-zA-Z0-9_-]+:[a-zA-Z0-9_.-]+)\s+a\s+skos:Concept\b",
        content,
    )
    if len(blocks) > 1:
        for i in range(1, len(blocks), 2):
            uri = _resolve_uri(blocks[i])
            body = blocks[i + 1]

            # Detect vocabulary from URI
            def _detect_vocab(u: str) -> str:
                from reconciliator.utils.curie import parse_curie

                src, _ = parse_curie(u)
                if src and src != "unknown":
                    return src.lower()
                for seg in reversed(u.rstrip("/").split("/")):
                    clean_seg = re.sub(r"[^a-zA-Z0-9_-]", "", seg)
                    if (
                        clean_seg
                        and not re.match(r"^p?\d+$", clean_seg, re.I)
                        and len(clean_seg) > 2
                    ):
                        return clean_seg.lower()
                return "vocab"

            vocab = _detect_vocab(uri)

            pref_matches = re.findall(r'skos:prefLabel\s+"([^"]+)"(?:@([a-zA-Z0-9_-]+))?', body)
            pref_labels: dict[str, str] = {}
            for lbl, lang in pref_matches:
                lang_code = (lang or "und").lower()
                if is_language_allowed(lang_code, pref_label_languages):
                    pref_labels[lang_code] = lbl

            alt_matches = re.findall(r'skos:altLabel\s+"([^"]+)"(?:@([a-zA-Z0-9_-]+))?', body)
            alt_labels: dict[str, list[str]] = {}
            for lbl, lang in alt_matches:
                lang_code = (lang or "und").lower()
                if is_language_allowed(lang_code, alt_label_languages):
                    alt_labels.setdefault(lang_code, []).append(lbl)

            is_deprecated = bool(
                re.search(r'owl:deprecated\s+(?:"true"\^\^xsd:boolean|true\b)', body, re.IGNORECASE)
            )

            active_target_fields = (
                vocab_target_fields
                if vocab_target_fields is not None
                else get_bundled_vocab_defaults().get("vocab_target_fields", {})
            )
            default_target_field = get_bundled_vocab_defaults()["default_vocab_target_field"]
            target_field = active_target_fields.get(vocab, default_target_field)

            concept = VocabConcept(
                uri=uri,
                vocabulary=vocab,
                pref_labels=pref_labels,
                alt_labels=alt_labels,
                is_deprecated=is_deprecated,
                target_field=target_field,
            )
            concepts.append(concept)

            # Check mappings
            matches = re.findall(
                r"(?:skos:exactMatch|dct:isReplacedBy)\s+(<[^>]+>|[a-zA-Z0-9_-]+:[a-zA-Z0-9_.-]+)",
                body,
            )
            for raw_match in matches:
                target_uri = _resolve_uri(raw_match)
                target_v = _detect_vocab(target_uri)
                t_field = active_target_fields.get(target_v, default_target_field)

                mapping = ConceptMapping(
                    legacy_uri=uri,
                    legacy_vocab=vocab,
                    legacy_labels=pref_labels,
                    target_uri=target_uri,
                    target_vocab=target_v,
                    mapping_type="exact_match",
                    target_field=t_field,
                )
                mappings.append(mapping)

            # Check hierarchical broader / narrower relations
            cid = concept.concept_id
            for bm in re.findall(r"skos:broader\s+([^;.]+)", body):
                for raw_b in re.findall(r"(<[^>]+>|[a-zA-Z0-9_-]+:[a-zA-Z0-9_.-]+)", bm):
                    parent_uri = _resolve_uri(raw_b)
                    pcid = extract_concept_id(parent_uri)
                    if pcid and pcid != cid:
                        hierarchy_edges.append(
                            {
                                "vocabulary": vocab,
                                "concept_id": cid,
                                "broader_concept_id": pcid,
                                "relation_type": "broader",
                            }
                        )

            for nm in re.findall(r"skos:narrower\s+([^;.]+)", body):
                for raw_n in re.findall(r"(<[^>]+>|[a-zA-Z0-9_-]+:[a-zA-Z0-9_.-]+)", nm):
                    child_uri = _resolve_uri(raw_n)
                    ncid = extract_concept_id(child_uri)
                    if ncid and ncid != cid:
                        hierarchy_edges.append(
                            {
                                "vocabulary": vocab,
                                "concept_id": ncid,
                                "broader_concept_id": cid,
                                "relation_type": "broader",
                            }
                        )

    return concepts, mappings, hierarchy_edges


def ingest_bridge_files(
    vocab_store: VocabStore,
    extra_bridge_dir: Optional[Path] = None,
    config: Optional[VocabIngestConfig] = None,
    tracker: Optional[ProgressTracker] = None,
) -> int:
    """Ingest bundled and optional custom bridge files into concept_mappings and concept_aliases."""
    bridge_files = get_bundled_bridge_files()
    if extra_bridge_dir and extra_bridge_dir.is_dir():
        bridge_files.extend(sorted(extra_bridge_dir.glob("*.ttl")))

    total_bridge_mappings: list[ConceptMapping] = []
    total_bridge_aliases: list[dict[str, Any]] = []
    total_bridge_relators: list[dict[str, Any]] = []

    cat_defaults = get_category_default_field_map()
    geo_field = cat_defaults["geographic"]
    topical_field = cat_defaults["topical"]
    vocab_defs = get_bundled_vocab_defaults()
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

            t_src, _ = parse_curie(target_uri)
            target_v = t_src if t_src and t_src != "unknown" else default_topical_vocab

            legacy_labels: dict[str, str] = dict(b.get("pref_labels") or {})
            for lang_key in pref_langs:
                if b.get(f"pref_label_{lang_key}") and lang_key not in legacy_labels:
                    legacy_labels[lang_key] = b[f"pref_label_{lang_key}"]

            total_bridge_mappings.append(
                ConceptMapping(
                    legacy_uri=source_uri,
                    legacy_vocab=stem,
                    legacy_labels=legacy_labels,
                    target_uri=target_uri,
                    target_vocab=target_v,
                    mapping_type="exact_match",
                    target_field=target_field,
                )
            )
            notation = b.get("notation")
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
                                    "uri": target_uri,
                                }
                            )
                if notation:
                    total_bridge_relators.append(
                        {
                            "folded_term": fold_label(notation),
                            "raw_term": notation,
                            "lang": "und",
                            "code": notation,
                            "uri": target_uri,
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
        sanitize_vocab_mappings(total_bridge_mappings, config=config, task_id=task_id, tracker=tracker)
        sanitize_entities(total_bridge_aliases, concept_aliases_table, config=config, task_id=task_id, tracker=tracker)
        sanitize_entities(total_bridge_relators, relator_terms_table, config=config, task_id=task_id, tracker=tracker)

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

    if tracker:
        tracker.start_task(stage="parsing_vocab")

    total_concepts: list[VocabConcept] = []
    total_mappings: list[ConceptMapping] = []
    total_hierarchy_edges: list[dict[str, Any]] = []

    for file_path in files:
        content = file_path.read_text(encoding="utf-8")
        concepts, mappings, edges = parse_turtle_simple(
            content,
            pref_label_languages=pref_langs,
            alt_label_languages=alt_langs,
            vocab_target_fields=vocab_target_fields,
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
