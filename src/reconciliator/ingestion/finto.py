"""FINTO ontology and migration tables ingestion pipeline into dedicated finto.db."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Optional

from reconciliator.finto_store import FintoStore
from reconciliator.models import FintoConcept, FintoMapping
from reconciliator.progress import ProgressTracker


def parse_turtle_simple(content: str) -> tuple[list[FintoConcept], list[FintoMapping]]:
    """
    Fast regex-based parser for SKOS Turtle (.ttl) concepts and mappings.
    Extracts skos:Concept, prefLabels, altLabels, exactMatch, and isReplacedBy.
    """
    concepts: list[FintoConcept] = []
    mappings: list[FintoMapping] = []

    # Block split by concept definition
    # e.g. <http://www.yso.fi/onto/yso/p1234> a skos:Concept ; ... .
    blocks = re.split(r"(?:^|\n)\s*<([^>]+)>\s+a\s+skos:Concept\b", content)
    if len(blocks) > 1:
        # blocks[0] is header/prefixes, then alternating (uri, body)
        for i in range(1, len(blocks), 2):
            uri = blocks[i].strip()
            body = blocks[i + 1]

            # Detect vocabulary from URI
            vocab = "yso"
            if "/kauno/" in uri:
                vocab = "kauno"
            elif "/muso/" in uri:
                vocab = "muso"
            elif ":slm:" in uri or "/slm/" in uri:
                vocab = "slm"
            elif ":seko:" in uri or "/seko/" in uri:
                vocab = "seko"

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

            target_field = "650"
            if vocab == "slm":
                target_field = "655"
            elif vocab == "seko":
                target_field = "382"

            concept = FintoConcept(
                uri=uri,
                vocabulary=vocab,
                pref_label_fi=pref_fi,
                pref_label_sv=pref_sv,
                pref_label_en=pref_en,
                alt_labels_fi=alt_fi,
                alt_labels_sv=alt_sv,
                target_field=target_field,
            )
            concepts.append(concept)

            # Check mappings
            # skos:exactMatch <http://www.yso.fi/onto/yso/p1234>
            matches = re.findall(r'(?:skos:exactMatch|dct:isReplacedBy)\s+<([^>]+)>', body)
            for target_uri in matches:
                target_v = "yso"
                if "/kauno/" in target_uri:
                    target_v = "kauno"
                elif "/muso/" in target_uri:
                    target_v = "muso"
                elif ":slm:" in target_uri or "/slm/" in target_uri:
                    target_v = "slm"
                elif ":seko:" in target_uri or "/seko/" in target_uri:
                    target_v = "seko"

                t_field = "650"
                if target_v == "slm":
                    t_field = "655"
                elif target_v == "seko":
                    t_field = "382"

                mapping = FintoMapping(
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


def ingest_finto_file(
    input_path: Path | str,
    finto_store: FintoStore,
    tracker: Optional[ProgressTracker] = None,
) -> tuple[int, int]:
    """Ingest FINTO Turtle or mapping file into finto.db."""
    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"FINTO input file not found: {path}")

    if tracker:
        tracker.start_task(stage="parsing_finto")

    content = path.read_text(encoding="utf-8")
    concepts, mappings = parse_turtle_simple(content)

    if tracker:
        tracker.update_progress(
            stage="inserting_concepts",
            items_total=len(concepts) + len(mappings),
            force=True,
        )

    finto_store.insert_concepts(concepts)
    finto_store.insert_mappings(mappings)

    if tracker:
        tracker.complete_task(
            metadata={"concepts_ingested": len(concepts), "mappings_ingested": len(mappings)}
        )

    return len(concepts), len(mappings)

