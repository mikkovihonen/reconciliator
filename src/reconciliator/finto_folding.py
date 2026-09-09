"""Finto subject heading reconciliation and legacy thesauri folding engine."""

from __future__ import annotations

from typing import Optional

from reconciliator.finto_store import FintoStore
from reconciliator.models import FintoConcept, FintoMapping, SubjectItem


LEGACY_TO_MODERN_DEFAULTS = {
    "ysa": "yso",
    "allars": "yso",
    "kaunokki": "kauno",
    "bella": "kauno",
    "musa": "muso",
    "cilla": "muso",
}


class FintoFolder:
    """Reconciles legacy subject thesauri headings to canonical modern FINTO ontologies."""

    def __init__(self, finto_store: Optional[FintoStore] = None) -> None:
        self.finto_store = finto_store

    def fold_subject_heading(
        self,
        heading: str,
        source_vocab: Optional[str] = None,
        source_field: str = "650",
        source_uri: Optional[str] = None,
        lang: str = "fi",
    ) -> tuple[str, str, str, str, str]:
        """
        Reconcile a subject heading to canonical modern ontology.

        Returns:
            (target_field, target_vocab, target_uri, target_pref_label, mapping_type)
        """
        clean_heading = heading.strip().rstrip(".,; ")
        norm_vocab = (source_vocab or "").strip().lower()

        # If already canonical modern ontology URI/code and no finto_store
        if self.finto_store is None:
            # Fallback heuristic if finto_store is not populated
            target_vocab = LEGACY_TO_MODERN_DEFAULTS.get(norm_vocab, norm_vocab or "yso")
            target_field = source_field
            if source_field == "655":
                target_vocab = "slm"
            elif source_field == "382":
                target_vocab = "seko"
            return target_field, target_vocab, source_uri or "", clean_heading, "none"

        # 1. Check legacy URI in finto_mappings
        if source_uri:
            mapping = self.finto_store.find_legacy_mapping_by_uri(source_uri)
            if mapping:
                concept = self.finto_store.find_concept_by_uri(mapping.target_uri)
                pref_label = clean_heading
                if concept:
                    pref_label = (
                        (concept.pref_label_sv if lang == "sv" else concept.pref_label_fi)
                        or concept.pref_label_fi
                        or concept.pref_label_en
                        or clean_heading
                    )
                target_field = mapping.target_field or source_field
                # Enforce: 655 is SLM
                if target_field == "655" and mapping.target_vocab == "kauno":
                    mapping.target_vocab = "slm"
                return target_field, mapping.target_vocab, mapping.target_uri, pref_label, mapping.mapping_type

        # 2. Check legacy label in finto_mappings
        mapping = self.finto_store.find_legacy_mapping_by_label(
            clean_heading, legacy_vocab=norm_vocab or None, lang=lang
        )
        if mapping:
            concept = self.finto_store.find_concept_by_uri(mapping.target_uri)
            pref_label = clean_heading
            if concept:
                pref_label = (
                    (concept.pref_label_sv if lang == "sv" else concept.pref_label_fi)
                    or concept.pref_label_fi
                    or clean_heading
                )
            target_field = mapping.target_field or source_field
            if target_field == "655" and mapping.target_vocab == "kauno":
                mapping.target_vocab = "slm"
            return target_field, mapping.target_vocab, mapping.target_uri, pref_label, mapping.mapping_type

        # 3. Direct lookup in finto_concepts
        candidate_vocabs: list[str] = []
        if source_field == "655":
            candidate_vocabs = ["slm"]
        elif source_field == "382":
            candidate_vocabs = ["seko"]
        elif source_field == "651":
            candidate_vocabs = ["yso"]
        else:
            # 650
            if norm_vocab in ("kaunokki", "bella"):
                candidate_vocabs = ["kauno", "yso"]
            elif norm_vocab in ("musa", "cilla"):
                candidate_vocabs = ["muso", "yso"]
            elif norm_vocab in ("ysa", "allars", "yso"):
                candidate_vocabs = ["yso"]
            else:
                candidate_vocabs = ["yso", "kauno", "muso"]

        for v in candidate_vocabs:
            concept = self.finto_store.find_concept_by_label(clean_heading, vocabulary=v, lang=lang)
            if concept:
                pref_label = (
                    (concept.pref_label_sv if lang == "sv" else concept.pref_label_fi)
                    or concept.pref_label_fi
                    or clean_heading
                )
                target_field = source_field
                # If term has a designated target_field (e.g. SLM genre term discovered in 650)
                if concept.target_field:
                    target_field = concept.target_field
                elif concept.vocabulary == "slm":
                    target_field = "655"
                elif concept.vocabulary == "seko":
                    target_field = "382"

                return target_field, concept.vocabulary, concept.uri, pref_label, "pref_label_lookup"

        # 4. Fallback if not found: keep as is
        default_target_vocab = LEGACY_TO_MODERN_DEFAULTS.get(norm_vocab, norm_vocab)
        if source_field == "655":
            default_target_vocab = "slm"
        elif source_field == "382":
            default_target_vocab = "seko"

        return source_field, default_target_vocab, source_uri or "", clean_heading, "none"

