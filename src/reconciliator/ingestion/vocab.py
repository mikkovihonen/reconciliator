"""Controlled vocabulary and migration tables ingestion pipeline into dedicated vocab.db."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from reconciliator.models import ConceptMapping, VocabConcept
from reconciliator.normalizers.linguistic import fold_label
from reconciliator.progress import ProgressTracker
from reconciliator.vocab.bridges import get_bundled_bridge_files, parse_bridge_turtle
from reconciliator.vocab_store import VocabStore


def parse_turtle_simple(content: str) -> tuple[list[VocabConcept], list[ConceptMapping]]:
    """
    Fast regex-based parser for SKOS Turtle (.ttl) concepts and mappings.
    Extracts skos:Concept, prefLabels, altLabels, exactMatch, and isReplacedBy.
    Supports both full URIs (<...>) and prefixed IRIs (@prefix ...).
    """
    concepts: list[VocabConcept] = []
    mappings: list[ConceptMapping] = []

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

    # Block split by concept definition
    blocks = re.split(
        r"(?:^|\n)\s*(<[^>]+>|[a-zA-Z0-9_-]+:[a-zA-Z0-9_.-]+)\s+a\s+skos:Concept\b",
        content,
    )
    if len(blocks) > 1:
        for i in range(1, len(blocks), 2):
            uri = _resolve_uri(blocks[i])
            body = blocks[i + 1]

            # Detect vocabulary from URI
            def _detect_vocab(u: str) -> str:
                lu = u.lower()
                if "/yso-paikat/" in lu or ":yso-paikat:" in lu:
                    return "yso-paikat"
                if "/kauno/" in lu or ":kauno:" in lu:
                    return "kauno"
                if "/kaunokki/" in lu or ":kaunokki:" in lu:
                    return "kaunokki"
                if "/muso/" in lu or ":muso:" in lu:
                    return "muso"
                if "/musa/" in lu or ":musa:" in lu:
                    return "musa"
                if "/slm/" in lu or ":slm:" in lu:
                    return "slm"
                if "/seko/" in lu or ":seko:" in lu:
                    return "seko"
                if "/ysa/" in lu or ":ysa:" in lu:
                    return "ysa"
                if "/allars/" in lu or ":allars:" in lu:
                    return "allars"
                if "/cilla/" in lu or ":cilla:" in lu:
                    return "cilla"
                if "/bella/" in lu or ":bella:" in lu:
                    return "bella"
                if "/mts/" in lu or ":mts:" in lu or "metatietosanasto" in lu:
                    return "mts"
                if "/yso/" in lu or ":yso:" in lu:
                    return "yso"
                # If custom or unknown, extract namespace segment
                for seg in reversed(u.rstrip("/").split("/")):
                    clean_seg = re.sub(r"[^a-zA-Z0-9_-]", "", seg)
                    if clean_seg and not clean_seg.isdigit() and len(clean_seg) > 2:
                        return clean_seg.lower()
                return "vocab"

            vocab = _detect_vocab(uri)

            pref_fi = None
            pref_sv = None
            pref_en = None
            m_fi = re.search(r'skos:prefLabel\s+"([^"]+)"@fi', body)
            if m_fi:
                pref_fi = m_fi.group(1)
            m_sv = re.search(r'skos:prefLabel\s+"([^"]+)"@sv', body)
            if m_sv:
                pref_sv = m_sv.group(1)
            m_en = re.search(r'skos:prefLabel\s+"([^"]+)"@en', body)
            if m_en:
                pref_en = m_en.group(1)

            alt_fi = re.findall(r'skos:altLabel\s+"([^"]+)"@fi', body)
            alt_sv = re.findall(r'skos:altLabel\s+"([^"]+)"@sv', body)

            is_deprecated = bool(
                re.search(r'owl:deprecated\s+(?:"true"\^\^xsd:boolean|true\b)', body, re.IGNORECASE)
            )

            target_field = "650"
            if vocab == "slm":
                target_field = "655"
            elif vocab == "seko":
                target_field = "382"
            elif vocab == "yso-paikat":
                target_field = "651"

            concept = VocabConcept(
                uri=uri,
                vocabulary=vocab,
                pref_label_fi=pref_fi,
                pref_label_sv=pref_sv,
                pref_label_en=pref_en,
                alt_labels_fi=alt_fi,
                alt_labels_sv=alt_sv,
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

                t_field = "650"
                if target_v == "slm":
                    t_field = "655"
                elif target_v == "seko":
                    t_field = "382"
                elif target_v == "yso-paikat":
                    t_field = "651"

                mapping = ConceptMapping(
                    legacy_uri=uri,
                    legacy_vocab=vocab,
                    legacy_label_fi=pref_fi,
                    legacy_label_sv=pref_sv,
                    target_uri=target_uri,
                    target_vocab=target_v,
                    mapping_type="exact_match",
                    target_field=t_field,
                )
                mappings.append(mapping)

    return concepts, mappings


def ingest_bridge_files(vocab_store: VocabStore, extra_bridge_dir: Optional[Path] = None) -> int:
    """Ingest bundled and optional custom bridge files into concept_mappings and concept_aliases."""
    bridge_files = get_bundled_bridge_files()
    if extra_bridge_dir and extra_bridge_dir.is_dir():
        bridge_files.extend(sorted(extra_bridge_dir.glob("*.ttl")))

    total_bridge_mappings: list[ConceptMapping] = []
    total_bridge_aliases: list[dict[str, Any]] = []

    for bf in bridge_files:
        stem = bf.stem.replace(".ttl", "")
        content = bf.read_text(encoding="utf-8")
        bridges = parse_bridge_turtle(content, bridge_vocab=stem)
        target_field = "651" if "countries" in stem else "650"

        for b in bridges:
            source_uri = b["source_uri"]
            target_uri = b["target_uri"]
            pref_fi = b.get("pref_label_fi")
            pref_sv = b.get("pref_label_sv")
            pref_en = b.get("pref_label_en")
            notation = b.get("notation")

            total_bridge_mappings.append(
                ConceptMapping(
                    legacy_uri=source_uri,
                    legacy_vocab=stem,
                    legacy_label_fi=pref_fi,
                    legacy_label_sv=pref_sv,
                    target_uri=target_uri,
                    target_vocab="yso",
                    mapping_type="exact_match",
                    target_field=target_field,
                )
            )

            for label, lang in ((pref_fi, "fi"), (pref_sv, "sv"), (pref_en, "en")):
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
                                "vocabulary": "yso",
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
                        "vocabulary": "yso",
                        "target_field": target_field,
                        "is_preferred": 0,
                        "source": "bridge",
                    }
                )

    if total_bridge_mappings:
        vocab_store.insert_mappings(total_bridge_mappings)
    if total_bridge_aliases:
        vocab_store.insert_aliases(total_bridge_aliases)

    return len(total_bridge_mappings)


def ingest_vocab_file(
    input_path: Path | str,
    vocab_store: VocabStore,
    tracker: Optional[ProgressTracker] = None,
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

    if tracker:
        tracker.start_task(stage="parsing_vocab")

    total_concepts: list[VocabConcept] = []
    total_mappings: list[ConceptMapping] = []

    for file_path in files:
        content = file_path.read_text(encoding="utf-8")
        concepts, mappings = parse_turtle_simple(content)
        total_concepts.extend(concepts)
        total_mappings.extend(mappings)

    if tracker:
        tracker.update_progress(
            stage="inserting_concepts",
            items_total=len(total_concepts) + len(total_mappings),
            force=True,
        )

    vocab_store.insert_concepts(total_concepts)
    vocab_store.insert_mappings(total_mappings)

    # Ingest MTS contributor roles into relator_terms
    mts_roles: list[dict[str, Any]] = []
    for c in total_concepts:
        if c.vocabulary == "mts":
            for lbl, lang in (
                (c.pref_label_fi, "fi"),
                (c.pref_label_sv, "sv"),
                (c.pref_label_en, "en"),
            ):
                if lbl:
                    folded = fold_label(lbl)
                    if folded:
                        mts_roles.append(
                            {"folded_term": folded, "raw_term": lbl, "lang": lang, "uri": c.uri}
                        )
    if mts_roles:
        vocab_store.insert_relators(mts_roles)

    # Ingest bundled bridge vocabularies
    ingest_bridge_files(vocab_store)

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

